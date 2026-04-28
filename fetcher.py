"""
fetcher.py - HTTP/PDF content retrieval for RFPScout.

Responsibilities:
  - Fetch a URL and return cleaned text ready for the LLM extractor.
  - Detect HTML vs PDF and route to the appropriate parser.
  - Strip navigation/footer noise from HTML so the extractor is not
    distracted by site chrome.
  - Truncate the final text to MAX_TEXT_CHARS to keep extractor token
    cost predictable.
  - Never raise to the caller — return an error dict so agent.py can
    skip the URL and continue.

Design decisions:
  - PDF detection uses two signals: the Content-Type header and the
    presence of '.pdf' in the URL path. Either is enough. Some servers
    serve PDFs with text/html headers; some URLs end in .pdf?token=...
    so we check the path component via urlparse rather than a naive
    endswith() on the full URL.
  - PDFs are streamed into a BytesIO buffer rather than written to disk.
    This keeps fetcher.py side-effect free and avoids cleanup logic.
  - Scanned PDFs return error="scanned_pdf" with no text. OCR is a V2
    enhancement; in V1 we surface the failure and move on rather than
    silently passing empty text to the LLM.
  - Retries fire only on transient failures: timeouts and HTTP 5xx.
    HTTP 4xx (404, 403, etc.) are permanent and not worth retrying.
  - A browser-like User-Agent reduces 403 rejections from nonprofit
    CMSes that block default Python clients. The string identifies
    RFPScout honestly rather than impersonating Chrome.
  - Final text is truncated to 8000 chars. Most RFPs fit in well under
    that, and the extractor is cheaper and more accurate when it does
    not see boilerplate footer text from long pages.
"""

import io
import logging
import time
from typing import Optional
from urllib.parse import urlparse

import pdfplumber
import requests
from bs4 import BeautifulSoup

from config import MAX_RETRIES, REQUEST_TIMEOUT

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum characters of cleaned text returned to the extractor. Keeping this
# tight controls LLM cost and avoids pushing the prompt close to the model's
# context window. Most RFP bodies fit comfortably under this.
MAX_TEXT_CHARS = 8000

# Honest UA: identifies the tool, but the Mozilla/5.0 prefix avoids 403s from
# CMSes that block "python-requests/x.x" by default.
USER_AGENT = "Mozilla/5.0 (compatible; RFPScout/1.0)"

# Tags whose content is almost never RFP body text. Stripping them before
# get_text() gives the extractor cleaner input.
NOISE_TAGS = ("nav", "header", "footer", "script", "style", "noscript", "form")

# HTTP status codes worth retrying. Permanent client errors (4xx) are not
# included — retrying them just wastes time and quota.
RETRYABLE_STATUS = {500, 502, 503, 504}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch(url: str) -> dict:
    """
    Fetch a URL and return cleaned text plus metadata.

    Args:
        url: The URL to fetch. Should be a fully-qualified http(s) URL.

    Returns:
        A dict with the following shape, regardless of outcome:
        {
          "url":         str,
          "text":        str | None,
          "source_type": "html" | "pdf" | None,
          "error":       None | str,   # short reason, e.g. "timeout", "http_404", "scanned_pdf"
        }

        On success: text is the cleaned body, error is None.
        On failure: text is None, error is a short string the caller
        can log. source_type may still be set if the failure happened
        after content type was determined (e.g. scanned_pdf).
    """
    if not url or not isinstance(url, str):
        return _error(url, "invalid_url")

    headers = {"User-Agent": USER_AGENT}
    looks_like_pdf = _url_path_is_pdf(url)

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )
        except requests.exceptions.Timeout:
            wait = 2 ** attempt
            logger.warning(
                "Timeout fetching %s (attempt %d/%d) — retrying in %ds",
                url, attempt + 1, MAX_RETRIES + 1, wait,
            )
            time.sleep(wait)
            continue
        except requests.exceptions.RequestException as exc:
            # Connection refused, DNS failure, SSL error, etc. — not worth retrying.
            logger.error("Network error fetching %s: %s", url, exc)
            return _error(url, f"network_error:{type(exc).__name__}")

        # Non-200 handling
        if response.status_code != 200:
            if response.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                wait = 2 ** attempt
                logger.warning(
                    "HTTP %d fetching %s (attempt %d/%d) — retrying in %ds",
                    response.status_code, url, attempt + 1, MAX_RETRIES + 1, wait,
                )
                time.sleep(wait)
                continue
            logger.warning("HTTP %d fetching %s — giving up", response.status_code, url)
            return _error(url, f"http_{response.status_code}")

        # Success — decide html vs pdf based on header + URL hint
        content_type = (response.headers.get("Content-Type") or "").lower()
        is_pdf = "application/pdf" in content_type or looks_like_pdf

        try:
            if is_pdf:
                text = _parse_pdf(response.content)
                if not text:
                    return _error(url, "scanned_pdf", source_type="pdf")
                return _ok(url, text, "pdf")
            else:
                text = _parse_html(response.text)
                if not text:
                    return _error(url, "empty_html", source_type="html")
                return _ok(url, text, "html")
        except Exception as exc:
            # Defensive: parsing libraries can raise on malformed input.
            # We catch broadly here so one bad page doesn't kill the run.
            logger.error("Parse error for %s: %s", url, exc)
            return _error(url, f"parse_error:{type(exc).__name__}")

    # Exhausted retries on timeout
    logger.error("Failed to fetch %s after %d attempts", url, MAX_RETRIES + 1)
    return _error(url, "max_retries_exceeded")


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_html(html: str) -> str:
    """
    Convert raw HTML to a single cleaned text string.

    - Strips nav/header/footer/script/style/form/noscript tags so the
      extractor sees body text only.
    - Joins remaining text with single spaces and trims whitespace.
    - Truncates to MAX_TEXT_CHARS.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(NOISE_TAGS):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return text[:MAX_TEXT_CHARS]


def _parse_pdf(pdf_bytes: bytes) -> str:
    """
    Extract text from PDF bytes using pdfplumber.

    Returns an empty string if the PDF contains no extractable text
    (typically a scanned image PDF). The caller distinguishes this
    from a successful parse by checking truthiness.
    """
    buffer = io.BytesIO(pdf_bytes)
    with pdfplumber.open(buffer) as pdf:
        pages_text = [page.extract_text() or "" for page in pdf.pages]
    text = "\n".join(pages_text).strip()
    return text[:MAX_TEXT_CHARS]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _url_path_is_pdf(url: str) -> bool:
    """
    Return True if the URL path component ends in .pdf or contains
    .pdf as a path segment. Query strings and fragments are ignored
    so URLs like https://example.org/files/rfp.pdf?token=... are
    still detected correctly.
    """
    try:
        path = urlparse(url).path.lower()
    except ValueError:
        return False
    return path.endswith(".pdf") or ".pdf/" in path


def _ok(url: str, text: str, source_type: str) -> dict:
    return {
        "url": url,
        "text": text,
        "source_type": source_type,
        "error": None,
    }


def _error(url: str, reason: str, source_type: Optional[str] = None) -> dict:
    return {
        "url": url,
        "text": None,
        "source_type": source_type,
        "error": reason,
    }