"""
gmail_client.py - Thin Gmail draft creation for RFPScout.

Responsibilities:
  - Authenticate with Gmail via OAuth (browser flow on first run, then
    cached in token.json).
  - Create a draft email in the authenticated user's Drafts folder.
  - Surface a named GmailAuthError so drafter.py can downgrade to
    local-only when Gmail is unavailable.

Design decisions:
  - The service object is built lazily on first use and cached for the
    process. drafter.py calls save_draft() once per record; sharing one
    service object across calls avoids re-authenticating per draft.
  - The HTML body conversion is borrowed from NonprofitReach. Plain-text
    drafts look unprofessional in Gmail's preview pane; the lightweight
    paragraph + bullet conversion gives a respectable rendering without
    forcing the LLM to emit HTML directly.
  - GmailAuthError wraps any failure during the OAuth flow or service
    construction. Operational failures (404, 5xx during draft creation)
    raise the underlying HttpError so they're not silently swallowed.
"""

import base64
import logging
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import CREDENTIALS_FILE, GMAIL_SCOPES, TOKEN_FILE

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class GmailAuthError(Exception):
    """
    Raised when the Gmail OAuth flow or service construction fails.

    drafter.py catches this specifically to downgrade to local-only
    drafting without swallowing other Gmail API errors (which we want
    to surface so a real bug doesn't pass silently).
    """


# ---------------------------------------------------------------------------
# Service construction
# ---------------------------------------------------------------------------

_service = None  # process-wide cache


def get_service():
    """
    Authenticate with Gmail and return a service object.

    On first run, opens a browser for user consent and writes token.json.
    On subsequent runs, refreshes the token silently if expired.

    Raises:
        GmailAuthError: If credentials.json is missing or OAuth fails.
    """
    global _service
    if _service is not None:
        return _service

    creds = _load_or_refresh_credentials()
    try:
        _service = build("gmail", "v1", credentials=creds)
    except Exception as exc:
        raise GmailAuthError(f"Failed to build Gmail service: {exc}") from exc
    return _service


def _load_or_refresh_credentials() -> Credentials:
    """
    Load token.json if present and valid; refresh if expired; otherwise
    run the browser-based OAuth flow.
    """
    creds: Optional[Credentials] = None

    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
        except Exception as exc:
            logger.warning("Could not load %s: %s", TOKEN_FILE, exc)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            logger.info("Gmail access token refreshed.")
            _save_token(creds)
            return creds
        except Exception as exc:
            logger.warning("Token refresh failed: %s — running full OAuth flow", exc)

    if not os.path.exists(CREDENTIALS_FILE):
        raise GmailAuthError(
            f"Gmail credentials not found at {CREDENTIALS_FILE}. "
            "Download OAuth client JSON from Google Cloud Console."
        )

    try:
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
        creds = flow.run_local_server(port=0)
    except Exception as exc:
        raise GmailAuthError(f"OAuth flow failed: {exc}") from exc

    _save_token(creds)
    logger.info("New Gmail credentials obtained via browser login.")
    return creds


def _save_token(creds: Credentials) -> None:
    try:
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    except OSError as exc:
        logger.warning("Could not save %s: %s", TOKEN_FILE, exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_draft(to: str, subject: str, body: str) -> str:
    """
    Create a draft email in the authenticated user's Drafts folder.

    Args:
        to:      Recipient email address.
        subject: Subject line.
        body:    Plain-text email body. Converted to lightweight HTML
                 internally for nicer Gmail rendering.

    Returns:
        The Gmail draft ID (a short string Gmail uses to reference the
        draft). drafter.py stores this on the record for traceability.

    Raises:
        GmailAuthError: If service construction fails.
        HttpError: If the Gmail API call itself fails (quota, 5xx, etc.).
    """
    service = get_service()

    message = MIMEMultipart()
    message["to"] = to
    message["subject"] = subject
    message.attach(MIMEText(_text_to_html(body), "html"))

    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()

    try:
        draft = (
            service.users()
            .drafts()
            .create(userId="me", body={"message": {"raw": encoded}})
            .execute()
        )
    except HttpError:
        # Re-raised so drafter.py can decide whether to skip this record
        # or abort the run. We don't want to silently drop these.
        raise

    draft_id = draft.get("id", "")
    logger.info("Gmail draft saved for %s | id: %s", to, draft_id)
    return draft_id


# ---------------------------------------------------------------------------
# Plain-text → HTML
# ---------------------------------------------------------------------------

def _text_to_html(body: str) -> str:
    """
    Convert plain-text email body to lightweight HTML.

    - Double newlines → paragraph breaks.
    - Lines starting with '•' → unordered list items.
    - Single newlines within a paragraph → <br>.

    This keeps drafts looking like email rather than a wall of monospaced
    text in Gmail's compose pane.
    """
    import html as html_lib

    paragraphs = body.split("\n\n")
    html_parts: list[str] = []

    for block in paragraphs:
        block = block.strip()
        if not block:
            continue

        lines = [ln for ln in block.splitlines() if ln.strip()]
        if all(ln.strip().startswith("•") for ln in lines):
            items = "".join(
                f"<li>{html_lib.escape(ln.strip().lstrip('•').strip())}</li>"
                for ln in lines
            )
            html_parts.append(f"<ul>{items}</ul>")
        else:
            escaped = html_lib.escape(block).replace("\n", "<br>")
            html_parts.append(f"<p>{escaped}</p>")

    return f"<html><body>{''.join(html_parts)}</body></html>"