"""
searcher.py - Google Custom Search API client for RFPScout.

Responsibilities:
  - Accept a list of query strings from query_builder.py.
  - Call the Google Custom Search JSON API and return structured result
    objects ready for fetcher.py.
  - Deduplicate URLs across all queries and all pages before returning.
  - In demo mode (SEARCH_PROVIDER == "demo"), return fixture results
    from examples/sample_search_results.json without making any API
    calls. This allows end-to-end pipeline testing without credentials.

Design decisions:
  - Results are deduplicated by URL (not title or snippet) because the
    same page can appear across multiple queries with different snippet
    text. URL is the canonical identity of a search result.
  - Pagination uses the `start` parameter (1, 11, 21...) rather than
    separate page numbers because that is what Google CSE expects. Each
    page yields up to 10 results.
  - Retry logic uses exponential backoff only for 429 (quota) and 503
    (service unavailable). Other HTTP errors (404, 403) are logged and
    skipped as retrying them is pointless.
  - A 0.5-second sleep between requests is conservative but avoids
    burning through the free CSE quota (100 queries/day) in a single
    run. This can be reduced if a paid tier is used.
  - Demo mode always returns the full fixture list regardless of how
    many queries or pages are requested. This keeps demo runs fast and
    predictable.
"""

import json
import logging
import time
from pathlib import Path

import requests

from config import (
    EXAMPLES_DIR,
    GOOGLE_API_KEY,
    GOOGLE_CSE_ID,
    MAX_RETRIES,
    REQUEST_TIMEOUT,
    SEARCH_PROVIDER,
)

logger = logging.getLogger(__name__)

# Google Custom Search JSON API endpoint
_CSE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"

# Delay between requests in seconds (live mode only)
_REQUEST_DELAY = 0.5

# HTTP status codes that are worth retrying
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Path to the demo fixture file
_FIXTURE_PATH = EXAMPLES_DIR / "sample_search_results.json"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search(queries: list[str], pages: int = 1) -> list[dict]:
    """
    Run all queries and return deduplicated search results.

    Args:
        queries: List of query strings from query_builder.build_queries().
        pages:   Number of result pages to fetch per query (each page = 10
                 results). Defaults to 1. Max meaningful value is 10
                 (Google CSE limit: 100 results per query).

    Returns:
        Deduplicated list of result dicts:
        [{"title": str, "snippet": str, "url": str}, ...]

    Notes:
        - In demo mode, the fixture file is returned regardless of
          queries or pages.
        - Results are deduplicated by URL. If the same URL appears in
          multiple queries, only the first occurrence is kept.
        - Returns an empty list (not an exception) if all queries fail.
    """
    if SEARCH_PROVIDER == "demo":
        return _load_fixture()

    all_results: list[dict] = []
    seen_urls: set[str] = set()

    for query in queries:
        query_results = _search_one_query(query, pages, seen_urls)
        all_results.extend(query_results)
        # Small delay between queries to respect CSE rate limits
        time.sleep(_REQUEST_DELAY)

    logger.info(
        "Search complete: %d unique results from %d quer%s",
        len(all_results),
        len(queries),
        "y" if len(queries) == 1 else "ies",
    )
    return all_results


# ---------------------------------------------------------------------------
# Single query execution
# ---------------------------------------------------------------------------

def _search_one_query(
    query: str,
    pages: int,
    seen_urls: set[str],
) -> list[dict]:
    """
    Execute one query for the requested number of pages.
    Updates seen_urls in-place for cross-query deduplication.
    Returns the results collected for this query.
    """
    results: list[dict] = []

    for page in range(pages):
        start = page * 10 + 1   # Google CSE: start=1 for page 1, start=11 for page 2
        page_results = _fetch_page(query, start)

        if page_results is None:
            # Unrecoverable error for this page — skip remaining pages for this query
            logger.warning("Aborting remaining pages for query: %.80s", query)
            break

        new_results = _deduplicate(page_results, seen_urls)
        results.extend(new_results)

        # If Google returned fewer than 10 results, there are no more pages
        if len(page_results) < 10:
            break

        time.sleep(_REQUEST_DELAY)

    return results


# ---------------------------------------------------------------------------
# Single page fetch with retry
# ---------------------------------------------------------------------------

def _fetch_page(query: str, start: int) -> list[dict] | None:
    """
    Fetch one page of results from Google CSE.

    Returns:
        List of raw result dicts, or None if the request fails after all
        retries. Returning None (rather than raising) lets the caller
        decide whether to skip or abort.
    """
    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CSE_ID,
        "q": query,
        "start": start,
    }

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.get(
                _CSE_ENDPOINT,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code == 200:
                return _parse_cse_response(response.json())

            if response.status_code in _RETRYABLE_STATUS:
                wait = 2 ** attempt
                logger.warning(
                    "CSE returned %d for query '%.60s' (attempt %d/%d) — "
                    "retrying in %ds",
                    response.status_code, query, attempt + 1, MAX_RETRIES + 1, wait,
                )
                time.sleep(wait)
                continue

            # Non-retryable error (403, 404, etc.)
            logger.error(
                "CSE returned %d for query '%.60s' — skipping",
                response.status_code, query,
            )
            return None

        except requests.exceptions.Timeout:
            wait = 2 ** attempt
            logger.warning(
                "CSE request timed out for query '%.60s' (attempt %d/%d) — "
                "retrying in %ds",
                query, attempt + 1, MAX_RETRIES + 1, wait,
            )
            time.sleep(wait)

        except requests.exceptions.RequestException as exc:
            logger.error("CSE request failed for query '%.60s': %s", query, exc)
            return None

    logger.error("CSE failed after %d attempts for query '%.60s'", MAX_RETRIES + 1, query)
    return None


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_cse_response(data: dict) -> list[dict]:
    """
    Extract result dicts from the Google CSE API response.

    Google CSE returns results under data["items"]. Each item has:
      - title    → display title of the page
      - snippet  → short text excerpt
      - link     → the page URL (we rename this to "url" for clarity)

    If "items" is absent (e.g. zero results for this query), an empty
    list is returned — this is a valid outcome, not an error.
    """
    items = data.get("items", [])
    results = []
    for item in items:
        url = item.get("link", "").strip()
        if not url:
            continue
        results.append({
            "title": item.get("title", "").strip(),
            "snippet": item.get("snippet", "").strip(),
            "url": url,
        })
    return results


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _deduplicate(results: list[dict], seen_urls: set[str]) -> list[dict]:
    """
    Filter results to only those whose URL has not been seen before.
    Updates seen_urls in-place so deduplication works across queries.
    """
    unique = []
    for result in results:
        url = result.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique.append(result)
    return unique


# ---------------------------------------------------------------------------
# Demo mode
# ---------------------------------------------------------------------------

def _load_fixture() -> list[dict]:
    """
    Load and return fixture search results from the examples directory.
    Used when SEARCH_PROVIDER == "demo" so the full pipeline can be
    tested without Google API credentials.
    """
    if not _FIXTURE_PATH.exists():
        logger.error(
            "Demo fixture not found at %s. "
            "Create examples/sample_search_results.json to use demo mode.",
            _FIXTURE_PATH,
        )
        return []

    try:
        with open(_FIXTURE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            logger.error("Fixture file must contain a JSON array. Got: %s", type(data))
            return []
        logger.info("Demo mode: loaded %d fixture results from %s", len(data), _FIXTURE_PATH)
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to load fixture file %s: %s", _FIXTURE_PATH, exc)
        return []