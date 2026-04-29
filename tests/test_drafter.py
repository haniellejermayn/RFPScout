"""
test_drafter.py - Manual drafter smoke test.

Pulls existing draft candidates from SQLite, runs them through the
drafter, and prints what landed in email_drafts.json. Cheaper than
running the full agent because it skips search/fetch/extract.

Usage: python test_drafter.py
"""

import json
import logging
from pathlib import Path

from config import DRAFT_THRESHOLD, EMAIL_DRAFTS_PATH
from drafter import generate_drafts
from storage import fetch_draft_candidates

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    candidates = fetch_draft_candidates(DRAFT_THRESHOLD)
    print(f"\nFound {len(candidates)} candidate(s) at score >= {DRAFT_THRESHOLD}:")
    for c in candidates:
        print(f"  [{c['confidence_score']:>3}] {c['org_name']} → {c['contact_email']}")

    if not candidates:
        print("\nNothing to draft. Need records with score >= 60 AND a valid contact_email.")
        return

    print(f"\nGenerating drafts (no Gmail) ...")
    summary = generate_drafts(candidates, run_id="manual-test", use_gmail=False)
    print(f"\nSummary: {summary}")

    if EMAIL_DRAFTS_PATH.exists():
        drafts = json.loads(Path(EMAIL_DRAFTS_PATH).read_text())
        print(f"\n--- Last entry in {EMAIL_DRAFTS_PATH.name} ---")
        print(json.dumps(drafts[-1], indent=2))


if __name__ == "__main__":
    main()