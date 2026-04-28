"""
test_scorer.py - Unit tests for the scorer module.

Strategy:
  - _make_record(**overrides) builds a 'perfect' baseline that scores ~97.
  - Each test overrides only the fields it cares about and asserts on
    either the absolute score or the delta from baseline.
  - Component-level tests vary one dimension at a time so a regression
    points to the broken component.
"""

from datetime import datetime, timedelta, timezone

from scorer import score


# ---------------------------------------------------------------------------
# Test factory
# ---------------------------------------------------------------------------

def _make_record(**overrides) -> dict:
    """
    Build a 'perfect' baseline record. Tests override only the fields
    they want to vary. Deadline defaults to 30 days from today (prime
    window) so a baseline call to score() returns the maximum.
    """
    deadline = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
    base = {
        "org_name": "Chicago Education Fund",
        "service_type": "marketing",
        "project_description": "Brand refresh and digital marketing for 2026 campaign.",
        "deadline_raw": deadline,
        "contact_email": "rfp@example.org",
        "budget_raw": "$50,000 - $80,000",
        "source_url": "https://example.org/rfps/marketing-2026",
        "source_type": "html",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Top-level scenarios from the handover spec
# ---------------------------------------------------------------------------

def test_perfect_record_scores_at_least_90():
    # 40 (completeness) + 30 (prime deadline) + 20 (budget) + 7 (html non-agg) = 97
    assert score(_make_record()) >= 90


def test_empty_record_scores_at_most_7():
    empty = {f: None for f in (
        "org_name", "service_type", "project_description", "deadline_raw",
        "contact_email", "budget_raw", "source_url", "source_type",
    )}
    # 0 (completeness) + 5 (no deadline) + 0 (no budget) + 2 (no url) = 7
    assert score(empty) <= 7


def test_expired_deadline_zeroes_deadline_component():
    expired = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
    record = _make_record(deadline_raw=expired)
    # baseline 97 - 30 (prime) + 0 (expired) = 67
    assert score(record) == 67


def test_aggregator_url_caps_source_component_at_4():
    record = _make_record(
        source_url="https://www.rfpdb.com/listing/12345",
        source_type="html",
    )
    # baseline 97 - 7 (html non-agg) + 4 (agg) = 94
    assert score(record) == 94


# ---------------------------------------------------------------------------
# Deadline tier checks
# ---------------------------------------------------------------------------

def test_no_deadline_scores_5_not_0():
    """Missing is better than expired — null gets the consolation 5 pts."""
    record = _make_record(deadline_raw=None)
    # completeness 7/7→6/7: int(6/7*40)=34, was 40, delta -6
    # deadline 30 → 5, delta -25
    # 97 - 6 - 25 = 66
    assert score(record) == 66


def test_within_14_days_scores_0_for_deadline():
    soon = (datetime.now(timezone.utc) + timedelta(days=7)).strftime("%Y-%m-%d")
    record = _make_record(deadline_raw=soon)
    # 97 - 30 + 0 = 67
    assert score(record) == 67


def test_61_to_120_days_scores_15():
    medium = (datetime.now(timezone.utc) + timedelta(days=90)).strftime("%Y-%m-%d")
    record = _make_record(deadline_raw=medium)
    # 97 - 30 + 15 = 82
    assert score(record) == 82


def test_far_future_deadline_scores_8():
    far = (datetime.now(timezone.utc) + timedelta(days=200)).strftime("%Y-%m-%d")
    record = _make_record(deadline_raw=far)
    # 97 - 30 + 8 = 75
    assert score(record) == 75


def test_unparseable_deadline_falls_back_to_5_pts():
    record = _make_record(deadline_raw="TBD")
    # deadline_raw is still 'filled' (non-empty string), so completeness stays at 40
    # 97 - 30 + 5 = 72
    assert score(record) == 72


def test_fuzzy_date_in_sentence_still_parses():
    """dateutil with fuzzy=True should pull the date out of surrounding words."""
    deadline = (datetime.now(timezone.utc) + timedelta(days=30))
    phrase = f"Proposals due by 5pm on {deadline.strftime('%B %d, %Y')}"
    record = _make_record(deadline_raw=phrase)
    # Should land in the prime tier (30 pts)
    assert score(record) == 97


# ---------------------------------------------------------------------------
# Source and budget checks
# ---------------------------------------------------------------------------

def test_pdf_source_scores_higher_than_html():
    html_record = _make_record(source_type="html")
    pdf_record = _make_record(source_type="pdf")
    assert score(pdf_record) - score(html_record) == 3   # 10 vs 7


def test_aggregator_pdf_still_scores_4():
    """Aggregator domain trumps source_type — 4 pts regardless of html/pdf."""
    record = _make_record(
        source_url="https://www.rfpdb.com/listing/12345.pdf",
        source_type="pdf",
    )
    # 40 + 30 + 20 + 4 = 94
    assert score(record) == 94


def test_no_budget_drops_score_by_26():
    """Removing budget_raw drops both budget (20) and one completeness slot."""
    with_budget = score(_make_record())
    without = score(_make_record(budget_raw=None))
    # budget 20 → 0 = -20, completeness 40 → 34 = -6, total -26
    assert with_budget - without == 26


# ---------------------------------------------------------------------------
# Defensive checks
# ---------------------------------------------------------------------------

def test_score_clamps_to_0_to_100():
    """Final result must be in [0, 100] regardless of component arithmetic."""
    result = score(_make_record())
    assert 0 <= result <= 100


def test_unknown_source_type_with_url_scores_2():
    """If we don't recognise source_type, treat the source as low-confidence."""
    record = _make_record(source_type="xml")
    # 40 + 30 + 20 + 2 = 92
    assert score(record) == 92