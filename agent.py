"""
agent.py - CLI entry point and pipeline orchestrator for RFPScout.

Responsibilities:
  - Accept inputs via CLI flags or interactive prompts.
  - Run the full pipeline: query -> search -> fetch -> extract -> score
    -> dedupe -> upsert -> export -> draft.
  - Honour --demo by short-circuiting external dependencies (search and
    LLM extraction) so a reviewer can see end-to-end output without
    any credentials configured.
  - Log a clear summary at the end so the user can see what happened.

Design decisions:
  - argparse drives non-interactive runs; questionary fills gaps. If
    the user supplies --sector but not --service, only --service is
    prompted. This keeps the headless command in the README repeatable
    without forcing reviewers to remember every flag.
  - Pipeline is sequential, single-threaded. Async fetching would speed
    up live runs but adds complexity (rate limits, error aggregation)
    that V1 does not need. The bottleneck in practice is the LLM call,
    not the HTTP fetch.
  - --demo short-circuits both the search layer (already in searcher.py)
    and the extraction layer (here). With no GITHUB_TOKEN, the LLM
    call would fail; --demo instead reads structured fixture records
    from examples/sample_rfp_texts.json. Drafting is skipped in --demo
    because draft generation also requires the LLM.
  - Error handling tiers match the handover spec: search failure with
    no fallback is fatal, fetch/extract failures are skip-and-continue,
    storage failure is fatal (data loss is the worst outcome), export
    failure is a warning (the database is the source of truth).
  - run_id is a UUID4 so multiple concurrent runs cannot collide. It
    is logged in storage.runs and tagged on every record so a single
    run is auditable later.
  - Counters live in a stats dict so the final summary can show actual
    pipeline behaviour (how many were fetched? skipped? not_rfp?) not
    just the final saved count.
"""

import argparse
import logging
import sys
import uuid
from pathlib import Path
from typing import Optional

from config import (
    DRAFT_THRESHOLD,
    EXAMPLES_DIR,
    INCLUSION_THRESHOLD,
)
from deduper import deduplicate
from extractor import extract
from fetcher import fetch
from query_builder import (
    build_queries,
    list_sectors,
    list_services,
)
from scorer import score
from searcher import search
from storage import (
    fetch_draft_candidates,
    init_db,
    log_run_finish,
    log_run_start,
    upsert_many,
)
from writer import export_all, preview_record

logger = logging.getLogger(__name__)

# Path to the fixture file used by --demo to bypass the LLM extractor.
DEMO_RFP_TEXTS_PATH = EXAMPLES_DIR / "sample_rfp_texts.json"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """
    Entry point. Returns a process exit code so this can be wired into
    `sys.exit(main())` cleanly.
    """
    args = _parse_args()
    _setup_logging(args.verbose)

    # Demo mode is fully zero-config — no API keys, no .env required.
    # It exists so a reviewer can clone, run, and see output in seconds.
    if args.demo:
        return _run_demo()

    # Resolve sector/service/pages/drafts. Anything missing from CLI flags
    # is filled by questionary prompts.
    sector, service, pages, drafts_mode = _resolve_inputs(args)
    return _run_pipeline(sector, service, pages, drafts_mode, demo=False)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rfpscout",
        description="Find, extract, and score nonprofit RFPs from the web.",
    )
    parser.add_argument(
        "--sector",
        help=f"Nonprofit sector. One of: {', '.join(list_sectors())}.",
    )
    parser.add_argument(
        "--service",
        help=f"Service type being procured. One of: {', '.join(list_services())}.",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=1,
        help="Number of search result pages per query (default: 1, max ~10).",
    )
    parser.add_argument(
        "--drafts",
        choices=("none", "local", "gmail"),
        default="none",
        help="Draft mode: none (default), local-only, or local+Gmail.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run end-to-end against fixtures with no API keys required.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug-level logging.",
    )
    return parser.parse_args()


def _setup_logging(verbose: bool) -> None:
    """Root logger config. INFO by default; DEBUG with --verbose."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet down noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("pdfminer").setLevel(logging.WARNING)
    logging.getLogger("pdfplumber").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Input resolution (CLI flags + questionary prompts)
# ---------------------------------------------------------------------------

def _resolve_inputs(args: argparse.Namespace) -> tuple[str, str, int, str]:
    """
    Return (sector, service, pages, drafts_mode), prompting interactively
    only for fields the user did not supply on the command line.
    """
    sector = args.sector or _prompt_select("Which sector?", list_sectors())
    service = args.service or _prompt_select("Which service type?", list_services())
    pages = args.pages
    drafts_mode = args.drafts

    return sector, service, pages, drafts_mode


def _prompt_select(question: str, choices: list[str]) -> str:
    """
    Wrap questionary so the import is lazy. If questionary is missing
    we fall back to plain input() so the agent still runs in stripped
    environments.
    """
    try:
        import questionary
    except ImportError:
        print(f"{question}\n  Options: {', '.join(choices)}")
        while True:
            value = input("> ").strip().lower()
            if value in choices:
                return value
            print(f"  Please choose one of: {', '.join(choices)}")
    return questionary.select(question, choices=choices).ask()


# ---------------------------------------------------------------------------
# Demo mode
# ---------------------------------------------------------------------------

def _run_demo() -> int:
    """
    Zero-config end-to-end run. Uses fixture search results AND fixture
    extracted records, so neither Google CSE nor the LLM is called.
    Drafts are skipped (also LLM-dependent).
    """
    logger.info("Demo mode: end-to-end with fixtures, no API keys required.")
    return _run_pipeline(
        sector="education",          # arbitrary; only used for run audit
        service="marketing",
        pages=1,
        drafts_mode="none",          # drafts need the LLM
        demo=True,
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _run_pipeline(
    sector: str,
    service: str,
    pages: int,
    drafts_mode: str,
    demo: bool,
) -> int:
    """
    Execute the full RFPScout pipeline. Returns a process exit code.
    """
    run_id = str(uuid.uuid4())
    stats = _new_stats()

    # --- Step 1: storage init + run audit row ---
    try:
        init_db()
        log_run_start(run_id, sector, service, pages)
    except Exception as exc:
        logger.critical("Storage init failed: %s — cannot continue", exc)
        return 1

    logger.info(
        "Run %s: sector=%s service=%s pages=%d drafts=%s demo=%s",
        run_id[:8], sector, service, pages, drafts_mode, demo,
    )

    # --- Step 2: query generation ---
    try:
        queries = build_queries(sector, service)
    except ValueError as exc:
        logger.error("Could not build queries: %s", exc)
        log_run_finish(run_id, 0, 0, 0, status="failed")
        return 2

    logger.info("Built %d search queries", len(queries))

    # --- Step 3: search ---
    results = search(queries, pages=pages)
    stats["search_results"] = len(results)
    if not results:
        logger.warning("No search results returned. Nothing to do.")
        log_run_finish(run_id, 0, 0, 0, status="completed")
        return 0
    logger.info("Search returned %d unique results", len(results))

    # --- Steps 4-6: fetch + extract + score, per-URL ---
    records = _process_results(results, run_id, demo, stats)

    if not records:
        logger.info("No records met inclusion criteria after filtering.")
        log_run_finish(run_id, len(results), 0, stats["below_threshold"], status="completed")
        _print_summary(stats, saved=0)
        return 0

    # --- Step 7: deduplicate ---
    records = deduplicate(records)
    stats["after_dedup"] = len(records)
    logger.info("Deduplication: %d records remain", len(records))

    # --- Step 8: upsert ---
    try:
        counts = upsert_many(records)
    except Exception as exc:
        logger.critical("Storage upsert failed: %s", exc)
        log_run_finish(run_id, len(results), 0, stats["below_threshold"], status="failed")
        return 1

    stats.update(counts)
    saved = counts["inserted"] + counts["updated"]
    logger.info(
        "Storage: inserted=%d updated=%d skipped=%d",
        counts["inserted"], counts["updated"], counts["skipped"],
    )

    # --- Step 9: export ---
    try:
        export_summary = export_all()
        logger.info("Exported %d records to CSV/JSON", export_summary["records_exported"])
    except Exception as exc:
        # Exports are not critical path — DB is source of truth.
        logger.warning("Export failed: %s — continuing", exc)

    # --- Step 10: drafts (skipped in demo) ---
    if drafts_mode != "none" and not demo:
        _run_drafts(run_id, drafts_mode)
    elif demo and drafts_mode != "none":
        logger.info("Drafts skipped in demo mode (LLM is short-circuited).")

    # --- Wrap up ---
    log_run_finish(
        run_id,
        records_found=len(results),
        records_saved=saved,
        records_skipped=stats["below_threshold"],
        status="completed",
    )

    _print_summary(stats, saved=saved, records=records)
    return 0


# ---------------------------------------------------------------------------
# Per-URL processing (fetch + extract + score)
# ---------------------------------------------------------------------------

def _process_results(
    results: list[dict],
    run_id: str,
    demo: bool,
    stats: dict,
) -> list[dict]:
    """
    Walk every search result through fetch -> extract -> score. Records
    below INCLUSION_THRESHOLD or that fail extraction are dropped. The
    returned list is ready for deduplicate().
    """
    # In demo mode we load all fixture extractions up front and match
    # them by URL. Any URL not in the fixture file is silently skipped
    # — the fixture is meant to be paired with sample_search_results.json.
    demo_extractions = _load_demo_extractions() if demo else {}

    records: list[dict] = []
    for result in results:
        url = result.get("url")
        if not url:
            continue

        # --- Step 4: fetch (skipped in demo) ---
        if demo:
            extracted = demo_extractions.get(url)
            if not extracted:
                logger.debug("No demo fixture for %s — skipping", url)
                continue
            stats["fetched"] += 1
        else:
            fetched = fetch(url)
            if fetched["error"]:
                logger.debug("Fetch failed for %s: %s", url, fetched["error"])
                stats["fetch_failed"] += 1
                continue
            stats["fetched"] += 1

            # --- Step 5: extract ---
            extracted = extract(
                fetched["text"],
                source_url=url,
                source_type=fetched["source_type"],
            )

        # not_rfp: extractor explicitly flagged this as not a procurement page
        if extracted.get("not_rfp"):
            stats["not_rfp"] += 1
            continue

        # parse_error: LLM returned malformed JSON twice
        if extracted.get("parse_error"):
            stats["parse_error"] += 1
            continue

        stats["extracted"] += 1

        # --- Step 6: score ---
        confidence = score(extracted)
        extracted["confidence_score"] = confidence

        if confidence < INCLUSION_THRESHOLD:
            stats["below_threshold"] += 1
            logger.debug(
                "Below threshold (%d < %d): %s",
                confidence, INCLUSION_THRESHOLD, url,
            )
            continue

        extracted["run_id"] = run_id
        records.append(extracted)

    return records


# ---------------------------------------------------------------------------
# Demo fixture loading
# ---------------------------------------------------------------------------

def _load_demo_extractions() -> dict[str, dict]:
    """
    Load examples/sample_rfp_texts.json, indexed by source_url.

    Expected format: a JSON array of full extracted record dicts
    (matching what extractor.extract() would return). Each record must
    have a source_url field — that becomes the lookup key here.
    """
    import json

    if not DEMO_RFP_TEXTS_PATH.exists():
        logger.warning(
            "Demo fixture %s not found. Demo will produce no records.",
            DEMO_RFP_TEXTS_PATH,
        )
        return {}

    try:
        with open(DEMO_RFP_TEXTS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load demo fixture: %s", exc)
        return {}

    if not isinstance(data, list):
        logger.error("Demo fixture must be a JSON array; got %s", type(data))
        return {}

    indexed = {}
    for record in data:
        url = record.get("source_url")
        if url:
            # Set defaults so the record looks like a real extractor output
            record.setdefault("not_rfp", False)
            record.setdefault("parse_error", False)
            indexed[url] = record
    logger.info("Loaded %d demo extractions from fixture", len(indexed))
    return indexed


# ---------------------------------------------------------------------------
# Drafts (placeholder until drafter.py is built)
# ---------------------------------------------------------------------------

def _run_drafts(run_id: str, drafts_mode: str) -> None:
    """
    Trigger draft generation for high-confidence records.

    drafter.py is built after agent.py reaches first-run-green. Until
    then this is a stub that lists candidates and explains the next
    step. The pipeline still completes successfully.
    """
    candidates = fetch_draft_candidates(DRAFT_THRESHOLD)
    if not candidates:
        logger.info("No draft candidates met DRAFT_THRESHOLD=%d", DRAFT_THRESHOLD)
        return

    try:
        from drafter import generate_drafts
    except ImportError:
        logger.info(
            "Drafter not yet implemented. %d candidate(s) would be drafted: %s",
            len(candidates),
            ", ".join((c.get("org_name") or c["source_url"]) for c in candidates[:5]),
        )
        return

    use_gmail = drafts_mode == "gmail"
    summary = generate_drafts(candidates, run_id=run_id, use_gmail=use_gmail)
    logger.info(
        "Drafts: %d written (%d to Gmail) at %s",
        summary.get("drafts_written", 0),
        summary.get("gmail_drafts", 0),
        summary.get("path", ""),
    )


# ---------------------------------------------------------------------------
# Stats and summary printing
# ---------------------------------------------------------------------------

def _new_stats() -> dict:
    return {
        "search_results": 0,
        "fetched": 0,
        "fetch_failed": 0,
        "extracted": 0,
        "not_rfp": 0,
        "parse_error": 0,
        "below_threshold": 0,
        "after_dedup": 0,
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
    }


def _print_summary(stats: dict, saved: int, records: Optional[list[dict]] = None) -> None:
    """
    Final human-readable summary printed after the pipeline finishes.
    Prints stats first, then a one-line preview per saved record.
    """
    print()
    print("=" * 60)
    print("RFPScout Run Summary")
    print("=" * 60)
    print(f"  Search results found:        {stats['search_results']}")
    print(f"  Fetched successfully:        {stats['fetched']}")
    if stats["fetch_failed"]:
        print(f"  Fetch failures:              {stats['fetch_failed']}")
    print(f"  Extracted as RFPs:           {stats['extracted']}")
    if stats["not_rfp"]:
        print(f"  Flagged as not_rfp:          {stats['not_rfp']}")
    if stats["parse_error"]:
        print(f"  LLM parse errors:            {stats['parse_error']}")
    print(f"  Below inclusion threshold:   {stats['below_threshold']}")
    print(f"  After deduplication:         {stats['after_dedup']}")
    print(f"  Saved to database:           {saved} "
          f"(inserted={stats['inserted']}, updated={stats['updated']}, skipped={stats['skipped']})")
    print()
    if records:
        print("Top records:")
        for r in sorted(records, key=lambda x: -x.get("confidence_score", 0))[:10]:
            print("  " + preview_record(r))
    print()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(main())