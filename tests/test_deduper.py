"""
test_deduper.py - Unit tests for the deduper module.

Strategy:
  - _record(**overrides) builds a baseline RFP record. Tests vary
    only the fields they care about.
  - Both passes are exercised, plus the _merge_sources helper directly.
"""

from deduper import _merge_sources, deduplicate


def _record(**overrides) -> dict:
    base = {
        "org_name": "ABC Foundation",
        "service_type": "marketing",
        "source_url": "https://abc.org/rfp",
        "confidence_score": 70,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_input_returns_empty_list():
    assert deduplicate([]) == []


def test_single_record_passes_through_with_sources_initialised():
    out = deduplicate([_record()])
    assert len(out) == 1
    assert out[0]["sources_json"] == ["https://abc.org/rfp"]


# ---------------------------------------------------------------------------
# Pass 1: URL match
# ---------------------------------------------------------------------------

def test_pass_1_collapses_same_url():
    a = _record(confidence_score=80)
    b = _record(confidence_score=50)
    out = deduplicate([a, b])
    assert len(out) == 1
    assert out[0]["confidence_score"] == 80   # higher wins


def test_pass_1_url_normalisation_via_rfp_id():
    """Trailing slash and fragment shouldn't split into two records."""
    a = _record(source_url="https://abc.org/rfp")
    b = _record(source_url="https://abc.org/rfp/#section")
    out = deduplicate([a, b])
    assert len(out) == 1


# ---------------------------------------------------------------------------
# Pass 2: fuzzy org match
# ---------------------------------------------------------------------------

def test_pass_2_fuzzy_org_match_merges():
    """Different URLs, similar org names, same service type → merge."""
    a = _record(
        org_name="ABC Foundation",
        source_url="https://abc.org/rfp",
        confidence_score=80,
    )
    b = _record(
        org_name="ABC Foundation Inc.",
        source_url="https://www.rfpdb.com/listing/abc",
        confidence_score=60,
    )
    out = deduplicate([a, b])
    assert len(out) == 1
    assert out[0]["confidence_score"] == 80
    assert "https://abc.org/rfp" in out[0]["sources_json"]
    assert "https://www.rfpdb.com/listing/abc" in out[0]["sources_json"]


def test_pass_2_different_service_types_do_not_merge():
    """Same org, different service types → keep both."""
    a = _record(service_type="marketing", source_url="https://abc.org/rfp1")
    b = _record(service_type="website", source_url="https://abc.org/rfp2")
    out = deduplicate([a, b])
    assert len(out) == 2


def test_pass_2_dissimilar_names_do_not_merge():
    a = _record(org_name="Boys Club", source_url="https://x.org/1")
    b = _record(org_name="Girl Scouts of America", source_url="https://y.org/2")
    out = deduplicate([a, b])
    assert len(out) == 2


def test_pass_2_skips_when_org_name_is_none():
    """Null org_name should never trigger a fuzzy merge."""
    a = _record(org_name=None, source_url="https://x.org/1")
    b = _record(org_name=None, source_url="https://y.org/2")
    out = deduplicate([a, b])
    assert len(out) == 2


# ---------------------------------------------------------------------------
# Merge semantics
# ---------------------------------------------------------------------------

def test_merge_preserves_winner_fields():
    """All fields of the higher-confidence record come through."""
    a = _record(
        org_name="ABC Foundation",
        confidence_score=80,
        contact_email="winner@abc.org",
        source_url="https://abc.org/1",
    )
    b = _record(
        org_name="ABC Foundation Inc.",
        confidence_score=60,
        contact_email="loser@abc.org",
        source_url="https://abc.org/2",
    )
    out = deduplicate([a, b])
    assert out[0]["contact_email"] == "winner@abc.org"


def test_tie_score_keeps_first_record():
    """On a confidence tie, the earlier record is kept."""
    a = _record(org_name="ABC Foundation", contact_email="first@abc.org",
                source_url="https://abc.org/1", confidence_score=70)
    b = _record(org_name="ABC Foundation Inc.", contact_email="second@abc.org",
                source_url="https://abc.org/2", confidence_score=70)
    out = deduplicate([a, b])
    assert len(out) == 1
    assert out[0]["contact_email"] == "first@abc.org"


# ---------------------------------------------------------------------------
# _merge_sources helper
# ---------------------------------------------------------------------------

def test_merge_sources_dedupes_overlap():
    assert _merge_sources(["a", "b"], ["b", "c"]) == ["a", "b", "c"]


def test_merge_sources_handles_none():
    assert _merge_sources(None, ["a"]) == ["a"]
    assert _merge_sources(["a"], None) == ["a"]
    assert _merge_sources(None, None) == []


def test_merge_sources_handles_scalar_input():
    """Defensive: stray non-list values become single-element lists."""
    assert _merge_sources("a", ["b"]) == ["a", "b"]