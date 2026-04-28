"""
storage.py - SQLite persistence layer for RFPScout.

Responsibilities:
  - Create the database and tables on first use (idempotent).
  - Upsert RFP records: insert new, update existing only when the
    incoming record has a higher confidence_score or richer fields.
  - Log each agent run to the runs table for traceability.
  - Provide query helpers used by writer.py, drafter.py, and agent.py.

Design decisions:
  - rfp_id is a SHA-256 hash of the normalized source_url. Stable
    across runs, so the same opportunity is never duplicated.
  - sources_json stores a JSON array of all URLs that pointed to the
    same opportunity (populated by deduper.py before storage is called).
  - deadline_iso is stored as TEXT in YYYY-MM-DD format. SQLite has no
    native date type; TEXT sorts correctly for ISO dates.
  - All timestamps are UTC ISO 8601 strings.
  - The upsert strategy: on conflict, update only if the new
    confidence_score is higher OR the existing record has parse_error.
    This avoids overwriting good data with worse data on reruns.
"""

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import DB_PATH

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CREATE_RFPS = """
CREATE TABLE IF NOT EXISTS rfps (
    rfp_id              TEXT PRIMARY KEY,
    org_name            TEXT,
    org_type            TEXT,
    service_type        TEXT,
    project_description TEXT,
    budget_raw          TEXT,
    budget_min_usd      INTEGER,
    budget_max_usd      INTEGER,
    deadline_raw        TEXT,
    deadline_iso        TEXT,
    contact_name        TEXT,
    contact_email       TEXT,
    contact_phone       TEXT,
    source_url          TEXT NOT NULL,
    source_type         TEXT,
    confidence_score    INTEGER DEFAULT 0,
    sources_json        TEXT,
    parse_error         INTEGER DEFAULT 0,   -- 1 = LLM parse failed
    discovered_at       TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    run_id              TEXT
);
"""

_CREATE_RUNS = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    sector      TEXT,
    service     TEXT,
    pages       INTEGER,
    records_found    INTEGER DEFAULT 0,
    records_saved    INTEGER DEFAULT 0,
    records_skipped  INTEGER DEFAULT 0,
    status      TEXT DEFAULT 'running'   -- running | completed | failed
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_rfps_confidence ON rfps (confidence_score);",
    "CREATE INDEX IF NOT EXISTS idx_rfps_service ON rfps (service_type);",
    "CREATE INDEX IF NOT EXISTS idx_rfps_deadline ON rfps (deadline_iso);",
    "CREATE INDEX IF NOT EXISTS idx_rfps_run ON rfps (run_id);",
]


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """
    Open a connection with row_factory so rows come back as dict-like
    objects. Enables WAL mode for safer concurrent reads (e.g. if a
    writer.py export runs while the agent is still inserting).
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

def init_db(db_path: Path = DB_PATH) -> None:
    """
    Create tables and indexes if they do not already exist.
    Safe to call on every startup (all statements use IF NOT EXISTS).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.execute(_CREATE_RFPS)
        conn.execute(_CREATE_RUNS)
        for idx_sql in _CREATE_INDEXES:
            conn.execute(idx_sql)
        conn.commit()
    logger.debug("Database initialised at %s", db_path)


# ---------------------------------------------------------------------------
# rfp_id generation
# ---------------------------------------------------------------------------

def make_rfp_id(source_url: str) -> str:
    """
    Produce a stable rfp_id from a source URL.

    Normalization:
      - strip whitespace
      - lowercase scheme and host (URL path is case-sensitive, kept as-is)
      - strip trailing slash from the path
      - drop the fragment (#anchor) since it's irrelevant for identity

    Using SHA-256 keeps the ID short and consistent regardless of URL
    length. Full hex digest (64 chars) is used to avoid collisions.
    """
    url = source_url.strip()
    # Normalize scheme+host to lowercase; keep path case.
    if "://" in url:
        scheme_host, _, rest = url.partition("://")
        scheme, _, host_and_path = rest.partition("/")
        url = f"{scheme_host}://{scheme.lower()}/{host_and_path}"
    url = url.rstrip("/")
    # Drop fragment.
    url = url.split("#")[0]
    return hashlib.sha256(url.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def upsert_rfp(record: dict, db_path: Path = DB_PATH) -> str:
    """
    Insert or update a single RFP record.

    Update strategy:
      - If rfp_id does not exist: insert.
      - If rfp_id exists and new confidence_score > existing: update all
        mutable fields and set updated_at.
      - If rfp_id exists and new confidence_score <= existing: skip update
        UNLESS the existing record has parse_error=1 (bad data replaced
        by valid data is always an improvement).

    Returns:
      'inserted', 'updated', or 'skipped'
    """
    now = _utcnow()
    rfp_id = record.get("rfp_id") or make_rfp_id(record["source_url"])

    with _connect(db_path) as conn:
        existing = conn.execute(
            "SELECT confidence_score, parse_error FROM rfps WHERE rfp_id = ?",
            (rfp_id,),
        ).fetchone()

        if existing is None:
            _insert_rfp(conn, rfp_id, record, now)
            conn.commit()
            logger.debug("Inserted rfp_id=%s (%s)", rfp_id[:12], record.get("org_name"))
            return "inserted"

        existing_score = existing["confidence_score"] or 0
        existing_error = existing["parse_error"] or 0
        new_score = record.get("confidence_score", 0) or 0

        if new_score > existing_score or existing_error:
            _update_rfp(conn, rfp_id, record, now)
            conn.commit()
            logger.debug("Updated rfp_id=%s (%s)", rfp_id[:12], record.get("org_name"))
            return "updated"

        logger.debug(
            "Skipped rfp_id=%s (existing score %d >= new score %d)",
            rfp_id[:12], existing_score, new_score,
        )
        return "skipped"


def _insert_rfp(conn: sqlite3.Connection, rfp_id: str, r: dict, now: str) -> None:
    conn.execute(
        """
        INSERT INTO rfps (
            rfp_id, org_name, org_type, service_type, project_description,
            budget_raw, budget_min_usd, budget_max_usd,
            deadline_raw, deadline_iso,
            contact_name, contact_email, contact_phone,
            source_url, source_type,
            confidence_score, sources_json, parse_error,
            discovered_at, updated_at, run_id
        ) VALUES (
            :rfp_id, :org_name, :org_type, :service_type, :project_description,
            :budget_raw, :budget_min_usd, :budget_max_usd,
            :deadline_raw, :deadline_iso,
            :contact_name, :contact_email, :contact_phone,
            :source_url, :source_type,
            :confidence_score, :sources_json, :parse_error,
            :discovered_at, :updated_at, :run_id
        )
        """,
        {
            "rfp_id": rfp_id,
            "org_name": r.get("org_name"),
            "org_type": r.get("org_type"),
            "service_type": r.get("service_type"),
            "project_description": r.get("project_description"),
            "budget_raw": r.get("budget_raw"),
            "budget_min_usd": r.get("budget_min_usd"),
            "budget_max_usd": r.get("budget_max_usd"),
            "deadline_raw": r.get("deadline_raw"),
            "deadline_iso": r.get("deadline_iso"),
            "contact_name": r.get("contact_name"),
            "contact_email": r.get("contact_email"),
            "contact_phone": r.get("contact_phone"),
            "source_url": r.get("source_url", ""),
            "source_type": r.get("source_type"),
            "confidence_score": r.get("confidence_score", 0),
            "sources_json": _serialise_sources(r.get("sources_json")),
            "parse_error": 1 if r.get("parse_error") else 0,
            "discovered_at": r.get("discovered_at") or now,
            "updated_at": now,
            "run_id": r.get("run_id"),
        },
    )


def _update_rfp(conn: sqlite3.Connection, rfp_id: str, r: dict, now: str) -> None:
    conn.execute(
        """
        UPDATE rfps SET
            org_name            = :org_name,
            org_type            = :org_type,
            service_type        = :service_type,
            project_description = :project_description,
            budget_raw          = :budget_raw,
            budget_min_usd      = :budget_min_usd,
            budget_max_usd      = :budget_max_usd,
            deadline_raw        = :deadline_raw,
            deadline_iso        = :deadline_iso,
            contact_name        = :contact_name,
            contact_email       = :contact_email,
            contact_phone       = :contact_phone,
            source_type         = :source_type,
            confidence_score    = :confidence_score,
            sources_json        = :sources_json,
            parse_error         = :parse_error,
            updated_at          = :updated_at,
            run_id              = :run_id
        WHERE rfp_id = :rfp_id
        """,
        {
            "rfp_id": rfp_id,
            "org_name": r.get("org_name"),
            "org_type": r.get("org_type"),
            "service_type": r.get("service_type"),
            "project_description": r.get("project_description"),
            "budget_raw": r.get("budget_raw"),
            "budget_min_usd": r.get("budget_min_usd"),
            "budget_max_usd": r.get("budget_max_usd"),
            "deadline_raw": r.get("deadline_raw"),
            "deadline_iso": r.get("deadline_iso"),
            "contact_name": r.get("contact_name"),
            "contact_email": r.get("contact_email"),
            "contact_phone": r.get("contact_phone"),
            "source_type": r.get("source_type"),
            "confidence_score": r.get("confidence_score", 0),
            "sources_json": _serialise_sources(r.get("sources_json")),
            "parse_error": 1 if r.get("parse_error") else 0,
            "updated_at": now,
            "run_id": r.get("run_id"),
        },
    )


# ---------------------------------------------------------------------------
# Batch upsert (used by agent.py after deduplication)
# ---------------------------------------------------------------------------

def upsert_many(records: list[dict], db_path: Path = DB_PATH) -> dict:
    """
    Upsert a list of records. Returns a summary dict:
      {"inserted": N, "updated": N, "skipped": N}
    """
    counts = {"inserted": 0, "updated": 0, "skipped": 0}
    for record in records:
        try:
            result = upsert_rfp(record, db_path)
            counts[result] += 1
        except Exception as exc:
            logger.error("Failed to upsert record %s: %s", record.get("source_url"), exc)
    return counts


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def fetch_all(db_path: Path = DB_PATH) -> list[dict]:
    """Return all records ordered by confidence_score descending."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM rfps ORDER BY confidence_score DESC, discovered_at DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def fetch_above_threshold(threshold: int, db_path: Path = DB_PATH) -> list[dict]:
    """
    Return records with confidence_score >= threshold.
    Used by writer.py and drafter.py to filter out low-quality records.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM rfps
            WHERE confidence_score >= ?
              AND parse_error = 0
            ORDER BY confidence_score DESC, deadline_iso ASC
            """,
            (threshold,),
        ).fetchall()
    return [dict(row) for row in rows]


def fetch_by_id(rfp_id: str, db_path: Path = DB_PATH) -> Optional[dict]:
    """Return a single record by rfp_id, or None if not found."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM rfps WHERE rfp_id = ?", (rfp_id,)
        ).fetchone()
    return dict(row) if row else None


def fetch_draft_candidates(draft_threshold: int, db_path: Path = DB_PATH) -> list[dict]:
    """
    Return records eligible for draft generation:
      - confidence_score >= draft_threshold
      - parse_error = 0
      - contact_email is not null (we need somewhere to send the draft)
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM rfps
            WHERE confidence_score >= ?
              AND parse_error = 0
              AND contact_email IS NOT NULL
              AND contact_email != ''
            ORDER BY confidence_score DESC
            """,
            (draft_threshold,),
        ).fetchall()
    return [dict(row) for row in rows]


def count_records(db_path: Path = DB_PATH) -> int:
    """Return total record count."""
    with _connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM rfps").fetchone()[0]


# ---------------------------------------------------------------------------
# Run audit
# ---------------------------------------------------------------------------

def log_run_start(
    run_id: str,
    sector: str,
    service: str,
    pages: int,
    db_path: Path = DB_PATH,
) -> None:
    """
    Insert a row into the runs table when an agent run begins.
    Call this at the top of agent.py before the pipeline starts.
    """
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO runs (run_id, started_at, sector, service, pages)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, _utcnow(), sector, service, pages),
        )
        conn.commit()


def log_run_finish(
    run_id: str,
    records_found: int,
    records_saved: int,
    records_skipped: int,
    status: str = "completed",
    db_path: Path = DB_PATH,
) -> None:
    """
    Update the run row when the agent finishes (or fails).
    status should be 'completed' or 'failed'.
    """
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE runs SET
                finished_at      = ?,
                records_found    = ?,
                records_saved    = ?,
                records_skipped  = ?,
                status           = ?
            WHERE run_id = ?
            """,
            (_utcnow(), records_found, records_saved, records_skipped, status, run_id),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    """Current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _serialise_sources(sources) -> Optional[str]:
    """
    Normalize sources_json to a JSON string regardless of whether it
    arrives as a Python list, an already-serialized string, or None.
    """
    if sources is None:
        return None
    if isinstance(sources, str):
        return sources  # already serialized
    if isinstance(sources, list):
        return json.dumps(sources)
    return json.dumps([str(sources)])