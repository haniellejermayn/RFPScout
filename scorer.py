"""
scorer.py - Confidence scoring for RFPScout records.

Responsibilities:
  - Take an extracted RFP record and compute a 0-100 confidence score.
  - Keep scoring deterministic and side-effect free so tests are easy
    and reruns are stable.
  - Expose a single public function `score(record)`. Helper functions
    are underscore-prefixed but importable for unit tests.

Design decisions:
  - Score is the sum of four components: field completeness (40),
    deadline usefulness (30), budget presence (20), source quality (10).
    Maxima sum to exactly 100 so a "perfect" record is a clean ceiling.
  - Field completeness counts seven tracked fields. We don't include
    every column — fields like contact_phone or org_type are
    nice-to-have but not a quality signal worth weighing.
  - Deadline tiers are biased toward the "prime window" (15-60 days).
    Anything within 14 days is too close to ship a proposal against,
    and anything beyond 120 days is too speculative.
  - A null deadline scores 5 pts, not 0. A missing deadline is less
    bad than an expired one — the page may simply not state it, and
    the agency can still reach out to ask.
  - Aggregator domains score lower than direct sources because the
    same RFP often appears on the nonprofit's own site too. The
    direct source is more authoritative and worth surfacing first.
  - dateutil.parser is called with fuzzy=True so phrases like
    "Proposals due May 30, 2026" still parse. The risk of a stray
    misparse is acceptable; the alternative (failing to parse any
    string with surrounding words) loses too many real deadlines.
  - All thresholds are module-level constants so they can be tuned
    without touching the scoring logic.
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from dateutil import parser as date_parser

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Fields counted toward the completeness component. These are the seven
# fields that, together, make a record actionable for an agency. Other
# fields (org_type, contact_phone, budget_min/max_usd) are stored but
# don't contribute to scoring.
COMPLETENESS_FIELDS = (
    "org_name",
    "service_type",
    "project_description",
    "deadline_raw",
    "contact_email",
    "budget_raw",
    "source_url",
)

# Hardcoded aggregator domains. Records from these sources score lower
# because the same RFP often exists on the nonprofit's own site, which
# is more authoritative.
AGGREGATOR_DOMAINS = frozenset({
    "rfpdb.com",
    "bidnetdirect.com",
    "govwin.com",
    "rfpmart.com",
    "rfpconnect.com",
    "mygovwatch.com"
})

# Deadline tier thresholds in days from today.
DEADLINE_TOO_SOON_DAYS = 14
DEADLINE_PRIME_MAX_DAYS = 60
DEADLINE_GOOD_MAX_DAYS = 120


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score(record: dict) -> int:
    """
    Compute a 0-100 confidence score for an extracted RFP record.

    Args:
        record: Dict produced by extractor.extract(), with source_url
                and source_type populated.

    Returns:
        Integer 0-100, suitable for storage in rfps.confidence_score.
    """
    pts = (
        _score_completeness(record)
        + _score_deadline(record.get("deadline_raw"))
        + _score_budget(record.get("budget_raw"))
        + _score_source(record.get("source_url"), record.get("source_type"))
    )

    # Defensive clamp — should never fire if components stay within their
    # stated maxima, but it prevents a future tweak from leaking an
    # out-of-range value into storage.
    return max(0, min(100, pts))


# ---------------------------------------------------------------------------
# Component scorers
# ---------------------------------------------------------------------------

def _score_completeness(record: dict) -> int:
    """
    Field completeness — up to 40 pts.
    Counts how many of the seven tracked fields are non-null and
    non-empty, then scales linearly to 40.
    """
    filled = sum(1 for f in COMPLETENESS_FIELDS if _is_filled(record.get(f)))
    return int((filled / len(COMPLETENESS_FIELDS)) * 40)


def _score_deadline(deadline_raw: Optional[str]) -> int:
    """
    Deadline usefulness — up to 30 pts.
    Tiered by how far the deadline is from today (UTC):
      - parse fails or null     →  5 pts
      - past or <= 14 days      →  0 pts
      - 15-60 days (prime)      → 30 pts
      - 61-120 days             → 15 pts
      - > 120 days              →  8 pts
    """
    if not _is_filled(deadline_raw):
        return 5

    try:
        parsed = date_parser.parse(deadline_raw, fuzzy=True)
    except (ValueError, TypeError, OverflowError):
        logger.debug("Could not parse deadline_raw: %r", deadline_raw)
        return 5

    today = datetime.now(timezone.utc).date()
    deadline_date = parsed.date() if hasattr(parsed, "date") else parsed
    days_away = (deadline_date - today).days

    if days_away <= DEADLINE_TOO_SOON_DAYS:
        return 0
    if days_away <= DEADLINE_PRIME_MAX_DAYS:
        return 30
    if days_away <= DEADLINE_GOOD_MAX_DAYS:
        return 15
    return 8


def _score_budget(budget_raw: Optional[str]) -> int:
    """
    Budget presence — up to 20 pts.
    Binary: stated → 20 pts, missing → 0 pts. We don't try to weight
    by budget size because the LLM's parsed budget figures are noisy
    and a stated budget at any size is a useful filter signal.
    """
    return 20 if _is_filled(budget_raw) else 0


def _score_source(source_url: Optional[str], source_type: Optional[str]) -> int:
    """
    Source quality — up to 10 pts.
      - Direct PDF       (not aggregator) → 10 pts
      - Direct HTML      (not aggregator) →  7 pts
      - Aggregator domain                 →  4 pts
      - Missing url or source_type        →  2 pts
    """
    if not _is_filled(source_url) or source_type is None:
        return 2

    if _is_aggregator(source_url):
        return 4

    if source_type == "pdf":
        return 10
    if source_type == "html":
        return 7

    # Unknown source_type but URL is present — treat as low-confidence.
    return 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_filled(value) -> bool:
    """
    A value counts as 'filled' if it is not None and not an empty or
    whitespace-only string. Numeric zero counts as filled.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _is_aggregator(source_url: str) -> bool:
    """
    True if the URL's host matches a known aggregator domain. Matches
    the suffix so 'www.rfpdb.com' and 'sub.rfpdb.com' both count as
    'rfpdb.com', while 'notrfpdb.com' does not.
    """
    try:
        host = urlparse(source_url).netloc.lower()
    except ValueError:
        return False
    if host.startswith("www."):
        host = host[4:]
    return any(host == d or host.endswith("." + d) for d in AGGREGATOR_DOMAINS)