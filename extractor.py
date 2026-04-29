"""
extractor.py - LLM-based RFP field extraction for RFPScout.

Responsibilities:
  - Send fetched page/PDF text to the LLM.
  - Return a clean dict of RFP fields, {"not_rfp": True}, or a fallback
    schema with parse_error=True if JSON parsing fails.
  - Attach source_url and source_type to the returned record so the
    caller (agent.py) has a complete record ready for scoring.

Design decisions:
  - Temperature 0 is non-negotiable for structured extraction. Any
    temperature above 0 risks creative field-filling.
  - The client is initialised once at module load (not per call) to
    avoid repeated object creation during a batch run.
  - Retry logic is inside extract(): one automatic retry on malformed
    JSON before falling back to the error schema. A second API call on
    a bad response is cheaper than losing a valid record.
  - deadline_iso is NOT extracted here. The LLM is bad at normalising
    dates consistently. storage.py derives deadline_iso from deadline_raw
    using a deterministic parser.
  - budget_min_usd and budget_max_usd: the LLM is asked to parse these
    as integers, but the output is validated and cast here. If the cast
    fails, both values are set to null rather than crashing.
  - The OpenAI-compatible client works with GitHub Models, OpenAI, and
    any provider that follows the same API shape. Switching providers
    means changing the base_url and token in .env only.
"""

import json
import logging
from typing import Optional

from openai import OpenAI

from config import GITHUB_MODELS_BASE_URL, GITHUB_TOKEN, MAX_RETRIES, PARSER_MODEL
from templates import EXTRACTION_SYSTEM_PROMPT, extraction_user_prompt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Expected field names - used for validation and fallback schema
# ---------------------------------------------------------------------------

RFP_FIELDS = [
    "org_name",
    "org_type",
    "service_type",
    "project_description",
    "budget_raw",
    "budget_min_usd",
    "budget_max_usd",
    "deadline_raw",
    "contact_name",
    "contact_email",
    "contact_phone",
]

# Allowed values for controlled vocabulary fields.
# If the LLM returns something outside these sets, we normalise to a safe
# default rather than passing bad data downstream.
VALID_ORG_TYPES = {"charity", "foundation", "association", "government", "unknown"}
VALID_SERVICE_TYPES = {
    "marketing", "fundraising", "technology", "website",
    "consulting", "event", "pr", "other",
}


# ---------------------------------------------------------------------------
# Client initialisation
# ---------------------------------------------------------------------------

def _make_client() -> Optional[OpenAI]:
    """
    Build the OpenAI-compatible client.
    Returns None if no token is configured — callers handle this as a
    signal to skip LLM calls (demo mode or missing credentials).
    """
    if not GITHUB_TOKEN:
        logger.warning(
            "GITHUB_TOKEN not set. LLM extraction unavailable. "
            "Run with --demo to use fixtures instead."
        )
        return None
    return OpenAI(
        base_url=GITHUB_MODELS_BASE_URL,
        api_key=GITHUB_TOKEN,
    )


_client: Optional[OpenAI] = _make_client()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract(text: str, source_url: str, source_type: str = "html") -> dict:
    """
    Extract RFP fields from fetched text using the LLM.

    Args:
        text:        Cleaned page/PDF text from fetcher.py. Should already
                     be truncated to ~8000 chars before this is called.
        source_url:  URL where the text was fetched from.
        source_type: "html", "pdf", or "fixture".

    Returns one of three shapes:

    1. Valid extraction:
       {"org_name": "...", "service_type": "...", ..., "source_url": "...",
        "source_type": "...", "not_rfp": False, "parse_error": False}

    2. Not an RFP:
       {"not_rfp": True}
       Caller should discard this record immediately.

    3. Parse failure (after retry):
       {"parse_error": True, "source_url": "...", "source_type": "...",
        "org_name": None, ...all other fields None...}
       Record is stored with confidence_score=0 for debugging.
    """
    if _client is None:
        logger.debug("No LLM client — returning parse_error for %s", source_url)
        return _fallback_schema(source_url, source_type, reason="no_client")

    if not text or not text.strip():
        logger.debug("Empty text — returning parse_error for %s", source_url)
        return _fallback_schema(source_url, source_type, reason="empty_text")

    # First attempt
    raw = _call_llm(text)
    result = _parse_response(raw)

    if result is None:
        # One retry on malformed JSON
        logger.warning("Malformed JSON from LLM for %s — retrying once", source_url)
        raw = _call_llm(text)
        result = _parse_response(raw)

    if result is None:
        logger.error("Extraction failed after retry for %s", source_url)
        return _fallback_schema(source_url, source_type, reason="parse_error")

    # Discard non-RFP results immediately — no need to attach source fields
    if result.get("not_rfp"):
        logger.debug("LLM flagged not_rfp for %s", source_url)
        return {"not_rfp": True}

    # Validate and normalise the returned fields
    cleaned = _validate_and_clean(result)
    cleaned["source_url"] = source_url
    cleaned["source_type"] = source_type
    cleaned["not_rfp"] = False
    cleaned["parse_error"] = False

    logger.debug(
        "Extracted: org=%s service=%s deadline=%s from %s",
        cleaned.get("org_name"),
        cleaned.get("service_type"),
        cleaned.get("deadline_raw"),
        source_url,
    )
    return cleaned


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _call_llm(text: str) -> str:
    """
    Make a single completion call and return the raw response string.
    Raises on API error — the caller handles retries.
    """
    response = _client.chat.completions.create(
        model=PARSER_MODEL,
        temperature=0,
        messages=[
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": extraction_user_prompt(text)},
        ],
    )
    content = response.choices[0].message.content or ""

    # Log token usage if available — useful for cost monitoring
    if hasattr(response, "usage") and response.usage:
        logger.debug(
            "Tokens used — prompt: %d, completion: %d",
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
        )

    return content.strip()


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_response(raw: str) -> Optional[dict]:
    """
    Parse the LLM's raw string output into a Python dict.

    The prompt instructs the model to return raw JSON with no markdown,
    but models sometimes wrap output in ```json ... ``` anyway. We strip
    common wrappers before parsing.

    Returns None if parsing fails (signals the caller to retry).
    """
    if not raw:
        return None

    # Strip markdown code fences if present
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        # Remove opening fence (```json or ```)
        cleaned = cleaned.split("\n", 1)[-1]
        # Remove closing fence
        if cleaned.endswith("```"):
            cleaned = cleaned[: cleaned.rfind("```")]
        cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.debug("JSON decode error: %s | raw: %.200s", exc, raw)
        return None

    if not isinstance(parsed, dict):
        logger.debug("LLM returned non-dict JSON: %s", type(parsed))
        return None

    return parsed


# ---------------------------------------------------------------------------
# Validation and normalisation
# ---------------------------------------------------------------------------

def _validate_and_clean(data: dict) -> dict:
    """
    Normalise a successfully parsed LLM response.

    - Ensures all expected fields are present (fills missing with None).
    - Validates controlled vocabulary fields (org_type, service_type).
    - Casts budget integers safely — if the LLM returns "50000" as a
      string, we convert it; if it returns something unconstable, we
      set null.
    - Strips whitespace from string fields.
    """
    cleaned = {}

    for field in RFP_FIELDS:
        value = data.get(field)

        if isinstance(value, str):
            value = value.strip() or None  # empty string → None

        cleaned[field] = value

    # Filter out common anti-scrape placeholder strings
    EMAIL_PLACEHOLDERS = {
        "[email protected]",
        "[email\xa0protected]",   # sometimes uses non-breaking space
    }
    EMAIL_NOISE_PATTERNS = (
        "spambots",
        "javascript enabled",
        "email is being protected",
    )

    if cleaned.get("contact_email"):
        email_lower = cleaned["contact_email"].lower()
        if (cleaned["contact_email"] in EMAIL_PLACEHOLDERS or
            any(p in email_lower for p in EMAIL_NOISE_PATTERNS) or
            "@" not in cleaned["contact_email"]):
            cleaned["contact_email"] = None    

    # Validate org_type
    if cleaned["org_type"] and cleaned["org_type"].lower() not in VALID_ORG_TYPES:
        logger.debug(
            "Unexpected org_type '%s' — normalising to 'unknown'", cleaned["org_type"]
        )
        cleaned["org_type"] = "unknown"
    elif cleaned["org_type"]:
        cleaned["org_type"] = cleaned["org_type"].lower()

    # Validate service_type
    if cleaned["service_type"] and cleaned["service_type"].lower() not in VALID_SERVICE_TYPES:
        logger.debug(
            "Unexpected service_type '%s' — normalising to 'other'", cleaned["service_type"]
        )
        cleaned["service_type"] = "other"
    elif cleaned["service_type"]:
        cleaned["service_type"] = cleaned["service_type"].lower()

    # Safe integer cast for budget fields
    cleaned["budget_min_usd"] = _safe_int(cleaned.get("budget_min_usd"))
    cleaned["budget_max_usd"] = _safe_int(cleaned.get("budget_max_usd"))

    return cleaned


def _safe_int(value) -> Optional[int]:
    """
    Convert a value to int without crashing.
    Handles: int, float, numeric string, None.
    Returns None on anything that cannot be cleanly converted.
    """
    if value is None:
        return None
    try:
        return int(float(str(value).replace(",", "").replace("$", "").strip()))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Fallback schema
# ---------------------------------------------------------------------------

def _fallback_schema(source_url: str, source_type: str, reason: str = "parse_error") -> dict:
    """
    Return a minimal valid record shape when extraction fails.

    Stored in SQLite with parse_error=True and confidence_score=0.
    Excluded from CSV/JSON exports by fetch_above_threshold() but kept
    in the database so the failed URL is visible for debugging.
    """
    record = {field: None for field in RFP_FIELDS}
    record.update(
        {
            "source_url": source_url,
            "source_type": source_type,
            "not_rfp": False,
            "parse_error": True,
            "confidence_score": 0,
            "_fallback_reason": reason,   # internal only, not stored in DB
        }
    )
    return record