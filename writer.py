"""
writer.py - Export layer for RFPScout.

Responsibilities:
  - Read RFP records from SQLite (above the inclusion threshold).
  - Write rfps.csv (flat, human-readable, one row per record).
  - Write rfps.json (structured, downstream-friendly, sources_json
    deserialised back into a real array).
  - Both files are always regenerated from scratch after each run.
    They are never appended to. SQLite is the source of truth.

Design decisions:
  - CSV uses utf-8-sig encoding. The BOM prefix makes the file open
    correctly in Excel without garbled characters (useful when a
    reviewer or sales analyst inspects the output).
  - JSON is pretty-printed (indent=2) so it is readable in a text
    editor and diffable in Git.
  - sources_json is stored in SQLite as a JSON string. writer.py
    deserialises it back into a Python list before writing JSON so
    the downstream consumer gets a real array, not a string-inside-JSON.
  - Records with parse_error=1 are excluded from both exports. They
    are kept in the database for debugging but should not surface in
    reviewer or sales outputs.
  - The function returns a summary dict so agent.py can log what was
    exported without importing csv or json itself.
"""

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from config import CSV_PATH, INCLUSION_THRESHOLD, JSON_PATH
from storage import fetch_above_threshold

logger = logging.getLogger(__name__)

# Ordered column list for the CSV. Matches the rfps table schema.
# Keeping this explicit (rather than using dict.keys()) means column
# order is stable even if the schema grows later.
CSV_COLUMNS = [
    "rfp_id",
    "org_name",
    "org_type",
    "service_type",
    "project_description",
    "budget_raw",
    "budget_min_usd",
    "budget_max_usd",
    "deadline_raw",
    "deadline_iso",
    "contact_name",
    "contact_email",
    "contact_phone",
    "source_url",
    "source_type",
    "confidence_score",
    "sources_json",       # kept as raw JSON string in CSV for simplicity
    "discovered_at",
    "updated_at",
    "run_id",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def export_all(
    threshold: int = INCLUSION_THRESHOLD,
    csv_path: Path = CSV_PATH,
    json_path: Path = JSON_PATH,
) -> dict:
    """
    Regenerate rfps.csv and rfps.json from SQLite.

    Args:
        threshold:  Minimum confidence_score to include. Defaults to
                    INCLUSION_THRESHOLD from config.py.
        csv_path:   Output path for the CSV file.
        json_path:  Output path for the JSON file.

    Returns:
        {
          "records_exported": int,
          "csv_path": str,
          "json_path": str,
          "exported_at": str   # UTC ISO timestamp
        }
    """
    records = fetch_above_threshold(threshold)

    _write_csv(records, csv_path)
    _write_json(records, json_path)

    exported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    summary = {
        "records_exported": len(records),
        "csv_path": str(csv_path),
        "json_path": str(json_path),
        "exported_at": exported_at,
    }

    logger.info(
        "Exported %d record(s) → %s | %s",
        len(records), csv_path.name, json_path.name,
    )
    return summary


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

def _write_csv(records: list[dict], path: Path) -> None:
    """
    Write records to a CSV file.

    utf-8-sig is used so Excel opens the file correctly without a manual
    import step. The extrasaction='ignore' on DictWriter means extra keys
    in a record (e.g. parse_error, which is excluded from CSV_COLUMNS)
    are silently dropped rather than raising an error.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=CSV_COLUMNS,
            extrasaction="ignore",
        )
        writer.writeheader()

        for record in records:
            writer.writerow(_prepare_csv_row(record))

    logger.debug("CSV written: %s (%d rows)", path, len(records))


def _prepare_csv_row(record: dict) -> dict:
    """
    Prepare a single record for CSV output.

    - None values become empty strings so the CSV does not contain the
      literal text 'None'.
    - sources_json is left as the raw JSON string. It is one field in
      a flat file, and deserialising it would require multiple columns
      or a nested structure that CSV cannot cleanly represent.
    """
    row = {}
    for col in CSV_COLUMNS:
        value = record.get(col)
        row[col] = "" if value is None else value
    return row


# ---------------------------------------------------------------------------
# JSON writer
# ---------------------------------------------------------------------------

def _write_json(records: list[dict], path: Path) -> None:
    """
    Write records to a JSON file.

    sources_json is deserialised from its stored string form back into a
    Python list so the downstream consumer receives a proper JSON array
    instead of a string-within-JSON. If the stored value is malformed,
    it falls back to an empty list and logs a warning rather than
    crashing the export.

    parse_error is included in the JSON output (unlike the CSV) because
    a downstream agent might want to filter or flag it programmatically.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    output = [_prepare_json_record(r) for r in records]

    with open(path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    logger.debug("JSON written: %s (%d records)", path, len(records))


def _prepare_json_record(record: dict) -> dict:
    """
    Prepare a single record for JSON output.

    - sources_json string → Python list.
    - parse_error integer (0/1) → Python bool for cleaner JSON.
    - All other None values are kept as JSON null (not converted to "").
    """
    out = dict(record)

    # Deserialise sources_json
    raw_sources = out.get("sources_json")
    if raw_sources:
        try:
            out["sources_json"] = json.loads(raw_sources)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "Could not deserialise sources_json for rfp_id=%s; defaulting to []",
                out.get("rfp_id", "unknown"),
            )
            out["sources_json"] = []
    else:
        out["sources_json"] = []

    # Convert SQLite integer flag to bool
    out["parse_error"] = bool(out.get("parse_error", 0))

    return out


# ---------------------------------------------------------------------------
# Convenience: export a single-record preview (used in tests / demo mode)
# ---------------------------------------------------------------------------

def preview_record(record: dict) -> str:
    """
    Return a formatted single-record string for CLI display.
    Used by agent.py to print a summary line after each upsert.

    Example output:
        [82] Chicago Education Fund | marketing | deadline: 2026-05-30
    """
    score = record.get("confidence_score", 0)
    org = record.get("org_name") or "Unknown org"
    svc = record.get("service_type") or "unknown service"
    deadline = record.get("deadline_iso") or record.get("deadline_raw") or "no deadline"
    return f"[{score:>3}] {org} | {svc} | deadline: {deadline}"