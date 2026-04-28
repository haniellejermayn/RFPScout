"""
deduper.py - Two-pass deduplication for RFPScout records.

Responsibilities:
  - Collapse records that point to the same opportunity into a single
    record before they hit storage.
  - Pass 1: exact URL match via the same rfp_id hash that storage uses.
  - Pass 2: fuzzy org name match (rapidfuzz token_sort_ratio >= 85)
    constrained to records sharing a service_type.
  - Preserve the higher-confidence record's fields and merge the
    sources_json lists so the final record carries every URL where
    the opportunity was discovered.

Design decisions:
  - rfp_id is computed via storage.make_rfp_id() so the dedup key is
    identical to the storage layer's primary key. A duplicate that
    slipped past dedup would still collide at upsert, but catching it
    here means we never burn an LLM call twice for the same URL.
  - Pass 2 requires matching service_type because the same nonprofit
    can run multiple distinct RFPs in parallel (e.g. a marketing RFP
    and a website RFP). High name similarity alone is not enough.
  - token_sort_ratio (not simple ratio) is used so word reorderings
    like "Foundation ABC" vs "ABC Foundation" still match, and so
    minor suffixes like "Inc." don't sink an otherwise clean match.
  - Higher confidence wins on conflict. On a tie we keep the first
    record encountered — deterministic, no surprise re-ordering of
    inputs.
  - sources_json is normalised to a list[str] for every record (even
    singletons) so storage doesn't have to special-case the merge.
"""

import logging
from typing import Optional

from rapidfuzz import fuzz

from storage import make_rfp_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Fuzzy match threshold for Pass 2. 85 is tight enough to avoid merging
# obviously different orgs ("Boys Club" vs "Boys & Girls Club") but loose
# enough to catch suffix variations ("ABC Foundation" vs "ABC Foundation
# Inc.") and minor punctuation differences.
ORG_NAME_THRESHOLD = 85


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def deduplicate(records: list[dict]) -> list[dict]:
    """
    Run two passes of deduplication over a list of extracted records.

    Args:
        records: List of scored records ready for storage.

    Returns:
        A new list with duplicates merged. Each output record has:
          - All original fields (the higher-confidence record wins).
          - sources_json populated with every URL the opportunity was
            seen at, deduplicated and order-preserving.
    """
    if not records:
        return []

    # Normalise sources_json on every input record so downstream code
    # can rely on it being a list.
    normalised = [_normalise_record(r) for r in records]

    pass1 = _dedupe_by_url(normalised)
    pass2 = _dedupe_by_org(pass1)

    logger.info(
        "Dedup: %d input -> %d after URL pass -> %d after org pass",
        len(records), len(pass1), len(pass2),
    )
    return pass2


# ---------------------------------------------------------------------------
# Pass 1: URL hash
# ---------------------------------------------------------------------------

def _dedupe_by_url(records: list[dict]) -> list[dict]:
    """
    Group records by rfp_id (hash of normalised source_url) and merge.
    """
    by_id: dict[str, dict] = {}

    for record in records:
        rfp_id = record.get("rfp_id") or make_rfp_id(record.get("source_url", ""))
        record["rfp_id"] = rfp_id

        existing = by_id.get(rfp_id)
        if existing is None:
            by_id[rfp_id] = record
        else:
            by_id[rfp_id] = _merge(existing, record)

    return list(by_id.values())


# ---------------------------------------------------------------------------
# Pass 2: fuzzy org name + service type match
# ---------------------------------------------------------------------------

def _dedupe_by_org(records: list[dict]) -> list[dict]:
    """
    Compare every pair of records and merge those whose org names are
    fuzzy-equivalent and whose service_types match exactly.

    O(n²) but n is small (typically <100 per run). Keeping the merged
    record live in slot `i` and continuing the inner loop lets us pick
    up one level of transitivity for free.
    """
    # Work on a list we can mutate. None marks a slot whose record was
    # absorbed into another; we filter those out at the end.
    survivors: list[Optional[dict]] = list(records)

    for i in range(len(survivors)):
        a = survivors[i]
        if a is None:
            continue
        for j in range(i + 1, len(survivors)):
            b = survivors[j]
            if b is None:
                continue
            if _is_fuzzy_match(a, b):
                merged = _merge(a, b)
                survivors[i] = merged
                survivors[j] = None
                a = merged   # keep comparing the merged record to remaining items

    return [r for r in survivors if r is not None]


def _is_fuzzy_match(a: dict, b: dict) -> bool:
    """
    Two records are a fuzzy match iff:
      - Both have non-empty org_name
      - Both have non-empty service_type that match exactly
      - rapidfuzz.fuzz.token_sort_ratio(names) >= ORG_NAME_THRESHOLD
    """
    name_a = (a.get("org_name") or "").strip()
    name_b = (b.get("org_name") or "").strip()
    if not name_a or not name_b:
        return False

    svc_a = a.get("service_type")
    svc_b = b.get("service_type")
    if not svc_a or not svc_b or svc_a != svc_b:
        return False

    ratio = fuzz.token_sort_ratio(name_a, name_b)
    return ratio >= ORG_NAME_THRESHOLD


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------

def _merge(a: dict, b: dict) -> dict:
    """
    Merge two duplicate records into one.

    - Higher confidence_score wins all field values. On a tie, `a`
      wins (i.e. the record that was already there in Pass 1, or the
      earlier-indexed record in Pass 2).
    - sources_json is the union of both records' source lists.
    """
    score_a = a.get("confidence_score") or 0
    score_b = b.get("confidence_score") or 0

    winner, loser = (a, b) if score_a >= score_b else (b, a)

    merged = dict(winner)
    merged["sources_json"] = _merge_sources(
        winner.get("sources_json"),
        loser.get("sources_json"),
    )
    return merged


def _merge_sources(a, b) -> list[str]:
    """
    Union two source lists while preserving order and removing dupes.
    Accepts None or non-list inputs gracefully.
    """
    a_list = a if isinstance(a, list) else ([a] if a else [])
    b_list = b if isinstance(b, list) else ([b] if b else [])
    return list(dict.fromkeys(a_list + b_list))


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _normalise_record(record: dict) -> dict:
    """
    Ensure sources_json exists as a list. New records arriving from
    extractor.py won't have one yet — initialise it with the record's
    own source_url so the merge step always has something to combine.
    """
    out = dict(record)
    sources = out.get("sources_json")
    if not sources:
        url = out.get("source_url")
        out["sources_json"] = [url] if url else []
    elif not isinstance(sources, list):
        out["sources_json"] = [sources]
    return out