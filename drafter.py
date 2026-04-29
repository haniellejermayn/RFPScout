"""
drafter.py - Outreach email draft generation for RFPScout.

Responsibilities:
  - Take a list of high-confidence RFP records.
  - For each one with a contact email, ask the LLM to write a short
    outreach email body using OUTREACH_SYSTEM_PROMPT.
  - Append every result (success or fallback) to data/email_drafts.json
    so each agent run accumulates rather than overwriting.
  - Optionally save each draft to Gmail's Drafts folder so the user
    can review and send from their normal email client.

Design decisions:
  - Drafts append. The JSON file accumulates across runs. SQLite is
    still the system of record; the JSON file is a convenience for the
    user to scan or copy from. Overwriting it would lose history.
  - Each draft entry has a draft_status field: "gmail", "local_only",
    "no_contact", or "llm_failed". Inspecting the JSON shows exactly
    what happened per record without needing to cross-reference logs.
  - Gmail failure downgrades the whole run to local-only. If credentials
    are missing or auth fails on the first record, every subsequent
    record would also fail — re-running the OAuth flow per record would
    be much worse than one warning and a clean fallback.
  - LLM exceptions are caught defensively. Draft generation is bonus;
    a single API blip should never kill an otherwise successful run.
    The record gets draft_status="llm_failed" and we continue.
  - The OpenAI client is initialised once at module load (same pattern
    as extractor.py) so a batch of drafts shares one client.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from openai import OpenAI

from config import (
    DRAFTER_MODEL,
    EMAIL_DRAFTS_PATH,
    GITHUB_MODELS_BASE_URL,
    GITHUB_TOKEN,
)
from templates import OUTREACH_SYSTEM_PROMPT, draft_subject, outreach_user_prompt

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Client (mirrors extractor.py)
# ---------------------------------------------------------------------------

def _make_client() -> Optional[OpenAI]:
    if not GITHUB_TOKEN:
        logger.warning(
            "GITHUB_TOKEN not set — drafter cannot generate emails. "
            "Drafts will be skipped this run."
        )
        return None
    return OpenAI(base_url=GITHUB_MODELS_BASE_URL, api_key=GITHUB_TOKEN)


_client: Optional[OpenAI] = _make_client()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_drafts(
    records: list[dict],
    run_id: str,
    use_gmail: bool = False,
) -> dict:
    """
    Generate outreach email drafts for a list of RFP records.

    Args:
        records:   List of high-confidence records (typically the result
                   of fetch_draft_candidates(DRAFT_THRESHOLD)).
        run_id:    Current agent run UUID, stored on each draft entry
                   for traceability.
        use_gmail: If True, attempt to save each draft to the user's
                   Gmail Drafts folder. Falls back to local-only if
                   Gmail authentication fails.

    Returns:
        {
          "drafts_written": int,   # successfully generated and stored locally
          "gmail_drafts":   int,   # successfully saved to Gmail
          "skipped":        int,   # records skipped (no contact email)
          "path":           str,   # path to email_drafts.json
        }
    """
    if not records:
        logger.info("Drafter: no records to draft.")
        return _empty_summary()

    if _client is None:
        logger.warning("Drafter: no LLM client — skipping draft generation.")
        return _empty_summary()

    # Load existing drafts so we append rather than overwrite
    existing = _load_existing_drafts(EMAIL_DRAFTS_PATH)
    new_entries: list[dict] = []
    counts = {"drafts_written": 0, "gmail_drafts": 0, "skipped": 0, "llm_failed": 0}

    # Gmail availability is decided lazily on first attempt. If it fails,
    # we flip this off for the rest of the run and never retry.
    gmail_active = use_gmail

    for record in records:
        contact = record.get("contact_email")
        if not contact:
            new_entries.append(_make_entry(record, run_id, "no_contact"))
            counts["skipped"] += 1
            continue

        body = _generate_body(record)
        if body is None:
            new_entries.append(_make_entry(record, run_id, "llm_failed"))
            counts["llm_failed"] += 1
            continue

        subject = draft_subject(record)
        entry = _make_entry(record, run_id, "local_only", subject=subject, body=body)

        if gmail_active:
            draft_id, gmail_active = _try_gmail_draft(contact, subject, body, gmail_active)
            if draft_id:
                entry["draft_status"] = "gmail"
                entry["gmail_draft_id"] = draft_id
                counts["gmail_drafts"] += 1

        new_entries.append(entry)
        counts["drafts_written"] += 1

    _save_drafts(existing + new_entries, EMAIL_DRAFTS_PATH)

    logger.info(
        "Drafter: %d generated (%d to Gmail, %d skipped, %d LLM failures)",
        counts["drafts_written"],
        counts["gmail_drafts"],
        counts["skipped"],
        counts["llm_failed"],
    )

    return {
        "drafts_written": counts["drafts_written"],
        "gmail_drafts": counts["gmail_drafts"],
        "skipped": counts["skipped"],
        "path": str(EMAIL_DRAFTS_PATH),
    }


# ---------------------------------------------------------------------------
# LLM body generation
# ---------------------------------------------------------------------------

def _generate_body(record: dict) -> Optional[str]:
    """
    Single LLM call to produce the email body. Returns None on failure
    so the caller can mark the record as llm_failed without crashing.
    """
    try:
        response = _client.chat.completions.create(
            model=DRAFTER_MODEL,
            temperature=0.4,   # a touch of variation so drafts don't sound identical
            messages=[
                {"role": "system", "content": OUTREACH_SYSTEM_PROMPT},
                {"role": "user", "content": outreach_user_prompt(record)},
            ],
        )
    except Exception as exc:
        logger.error(
            "LLM error generating draft for %s: %s",
            record.get("source_url"), exc,
        )
        return None

    body = (response.choices[0].message.content or "").strip()
    if not body:
        logger.warning(
            "Empty body from LLM for %s — treating as failure",
            record.get("source_url"),
        )
        return None

    # Strip any bracketed placeholders the LLM left behind despite the prompt.
    # E.g. "[Your Name]", "[Agency Name]", "[Recipient's Name]"
    body = re.sub(r"\[(Your|Agency|Recipient'?s)[^\]]*\]\s*", "", body, flags=re.IGNORECASE)
    body = body.strip()
        
    return body


# ---------------------------------------------------------------------------
# Gmail integration
# ---------------------------------------------------------------------------

def _try_gmail_draft(
    to: str,
    subject: str,
    body: str,
    gmail_active: bool,
) -> tuple[Optional[str], bool]:
    """
    Attempt to save a Gmail draft. Returns (draft_id, gmail_active_after).

    On the first failure we flip gmail_active off so subsequent records
    don't keep retrying a broken auth flow. Distinguishes auth errors
    (downgrade entire run) from operational errors (skip this record).
    """
    if not gmail_active:
        return None, False

    try:
        from gmail_client import GmailAuthError, save_draft
    except ImportError:
        logger.warning("gmail_client not installed — falling back to local-only drafts")
        return None, False

    try:
        draft_id = save_draft(to=to, subject=subject, body=body)
        return draft_id, True
    except GmailAuthError as exc:
        # Auth failure → downgrade the rest of the run.
        logger.warning(
            "Gmail authentication failed (%s). Falling back to local-only drafts.",
            exc,
        )
        return None, False
    except Exception as exc:
        # Operational failure (404, 5xx, etc) → skip this record but keep trying others.
        logger.error("Gmail draft failed for %s: %s — saving local-only", to, exc)
        return None, True


# ---------------------------------------------------------------------------
# Entry construction
# ---------------------------------------------------------------------------

def _make_entry(
    record: dict,
    run_id: str,
    draft_status: str,
    subject: Optional[str] = None,
    body: Optional[str] = None,
) -> dict:
    """
    Build a single draft entry dict. Stored shape is intentionally flat
    and simple so a reviewer can scan email_drafts.json without needing
    to know the rest of the codebase.
    """
    return {
        "rfp_id": record.get("rfp_id"),
        "run_id": run_id,
        "org_name": record.get("org_name"),
        "service_type": record.get("service_type"),
        "contact_email": record.get("contact_email"),
        "subject": subject,
        "body": body,
        "draft_status": draft_status,
        "gmail_draft_id": None,
        "source_url": record.get("source_url"),
        "created_at": _utcnow(),
    }


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _load_existing_drafts(path: Path) -> list[dict]:
    """
    Return the contents of email_drafts.json, or [] if the file is
    missing or corrupt. Corruption is logged but doesn't crash the run.
    """
    if not path.exists():
        return []

    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            logger.warning("Drafts file is not a list — starting fresh")
            return []
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Drafts file unreadable (%s) — starting fresh", exc)
        return []


def _save_drafts(entries: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty_summary() -> dict:
    return {
        "drafts_written": 0,
        "gmail_drafts": 0,
        "skipped": 0,
        "path": str(EMAIL_DRAFTS_PATH),
    }