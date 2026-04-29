"""
searcher.py - Brave Search API client for RFPScout.

Responsibilities:
  - Accept a list of query strings from query_builder.py.
  - Call the Brave Search API and return structured result objects
    ready for fetcher.py.
  - Deduplicate URLs across all queries and all pages before returning.
  - In demo mode (SEARCH_PROVIDER == "demo"), return fixture results
    from examples/sample_search_results.json without making any API
    calls. This allows end-to-end pipeline testing without credentials.

Design decisions:
  - Brave was chosen over Google Programmable Search because Google
    closed the legacy CSE free tier. Brave's free tier (varies by
    account; the dashboard is the source of truth) is enough for V1.
    Switching providers means changing this file and the env vars;
    nothing else in the pipeline cares.
  - Auth is via the X-Subscription-Token header rather than a query
    parameter. Less risk of the key showing up in URL logs.
  - Brave paginates by page index (offset=0, 1, 2...) not item offset.
    We keep the public API of pages=N unchanged and translate
    internally so callers do not need to know the difference.
  - Brave caps offset at 9 (10 pages max). We clamp defensively so a
    caller passing pages=15 does not waste 5 calls returning 422.
  - Free-tier rate limit is 1 req/sec. _REQUEST_DELAY is 1.1s for a
    small safety margin against clock skew. This is the dominant
    runtime cost in a live run; a paid tier could reduce it.
  - Retry logic uses exponential backoff for 429 (rate limit) and 5xx.
    422 (malformed query) and other 4xx are logged and skipped because
    retrying them is pointless.
  - Demo mode returns the full fixture list regardless of how many
    queries or pages are requested. This keeps demo runs fast and
    predictable, and lets reviewers exercise the pipeline without a
    Brave key.
"""

import json
import logging
import time

import requests

from config import (
    BRAVE_API_KEY,
    EXAMPLES_DIR,
    MAX_RETRIES,
    REQUEST_TIMEOUT,
    SEARCH_PROVIDER,
)

logger = logging.getLogger(__name__)

# Brave Search API endpoint
_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

# Results requested per page. Brave accepts 1-20; we use 10 so existing
# pagination math (pages * 10 max results per query) is unchanged.
_RESULTS_PER_PAGE = 10

# Brave's hard cap on offset (0-9 = 10 pages max per query).
_MAX_OFFSET = 9

# Delay between requests in seconds. Brave free tier: 1 req/sec.
# 1.1 gives a small margin against clock skew and request overhead.
_REQUEST_DELAY = 1.1

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
                 results). Brave caps this at 10 (offset 0-9). Values
                 above 10 are silently clamped.

    Returns:
        Deduplicated list of result dicts:
        [{"title": str, "snippet": str, "url": str}, ...]

    Notes:
        - In demo mode, the fixture file is returned regardless of
          queries or pages.
        - Results are deduplicated by URL across all queries and pages.
        - Returns an empty list (not an exception) if all queries fail.
    """
    if SEARCH_PROVIDER == "demo":
        return _load_fixture()

    if not BRAVE_API_KEY:
        logger.error(
            "BRAVE_API_KEY not set. Either configure it in .env or run "
            "with SEARCH_PROVIDER=demo."
        )
        return []

    # Defensive clamp: Brave caps offset at 9 (10 pages total).
    effective_pages = min(pages, _MAX_OFFSET + 1)
    if effective_pages != pages:
        logger.warning(
            "pages=%d exceeds Brave's max (10) — clamping to 10",
            pages,
        )

    all_results: list[dict] = []
    seen_urls: set[str] = set()

    for query in queries:
        query_results = _search_one_query(query, effective_pages, seen_urls)
        all_results.extend(query_results)
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
        # Brave: offset is the page index (0, 1, 2...), not item offset.
        page_results = _fetch_page(query, offset=page)

        if page_results is None:
            logger.warning("Aborting remaining pages for query: %.80s", query)
            break

        new_results = _deduplicate(page_results, seen_urls)
        results.extend(new_results)

        # If Brave returned fewer than a full page, there are no more pages.
        if len(page_results) < _RESULTS_PER_PAGE:
            break

        time.sleep(_REQUEST_DELAY)

    return results


# ---------------------------------------------------------------------------
# Single page fetch with retry
# ---------------------------------------------------------------------------

def _fetch_page(query: str, offset: int) -> list[dict] | None:
    """
    Fetch one page of results from Brave Search.

    Returns:
        List of raw result dicts, or None if the request fails after all
        retries. Returning None (rather than raising) lets the caller
        decide whether to skip or abort.
    """
    headers = {
        "X-Subscription-Token": BRAVE_API_KEY,
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    }
    params = {
        "q": query,
        "count": _RESULTS_PER_PAGE,
        "offset": offset,
    }

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.get(
                _BRAVE_ENDPOINT,
                headers=headers,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code == 200:
                return _parse_brave_response(response.json())

            if response.status_code in _RETRYABLE_STATUS:
                wait = 2 ** attempt
                logger.warning(
                    "Brave returned %d for query '%.60s' (attempt %d/%d) — "
                    "retrying in %ds",
                    response.status_code, query, attempt + 1, MAX_RETRIES + 1, wait,
                )
                time.sleep(wait)
                continue

            # Non-retryable: 401 (bad key), 403 (forbidden), 422 (bad query), 404, etc.
            error_detail = _extract_brave_error(response)
            logger.error(
                "Brave returned %d for query '%.60s'%s — skipping",
                response.status_code, query,
                f" ({error_detail})" if error_detail else "",
            )
            return None

        except requests.exceptions.Timeout:
            wait = 2 ** attempt
            logger.warning(
                "Brave request timed out for query '%.60s' (attempt %d/%d) — "
                "retrying in %ds",
                query, attempt + 1, MAX_RETRIES + 1, wait,
            )
            time.sleep(wait)

        except requests.exceptions.RequestException as exc:
            logger.error("Brave request failed for query '%.60s': %s", query, exc)
            return None

    logger.error("Brave failed after %d attempts for query '%.60s'", MAX_RETRIES + 1, query)
    return None


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_brave_response(data: dict) -> list[dict]:
    """
    Extract result dicts from the Brave Search API response.

    Brave's response shape (simplified):
        {
          "web": {
            "results": [
              {"title": "...", "url": "...", "description": "...", ...},
              ...
            ]
          },
          ...
        }

    We map description -> snippet so the rest of the pipeline doesn't
    have to care which provider produced the result.

    If "results" is absent (zero results for this query), an empty list
    is returned — this is a valid outcome, not an error.
    """
    web = data.get("web") or {}
    items = web.get("results", [])
    results = []
    for item in items:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        results.append({
            "title": (item.get("title") or "").strip(),
            "snippet": (item.get("description") or "").strip(),
            "url": url,
        })
    return results


def _extract_brave_error(response: requests.Response) -> str:
    """
    Pull a useful error message from a Brave error response if possible.
    Falls back to empty string if the body isn't JSON or doesn't match
    the expected shape.
    """
    try:
        body = response.json()
        err = body.get("error") or {}
        return err.get("detail") or err.get("code") or ""
    except (ValueError, AttributeError):
        return ""


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
    tested without Brave API credentials.
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