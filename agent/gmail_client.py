"""Gmail API client: OAuth + read + (later) send.

First run opens a browser to authorize; subsequent runs reuse token.json.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from email import message_from_bytes
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Read + send. Send is added now so we don't have to re-consent later when
# Phase 3 wires up application sending.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

ROOT = Path(__file__).resolve().parent.parent
CREDENTIALS_PATH = ROOT / "credentials.json"
TOKEN_PATH = ROOT / "token.json"


@dataclass
class FetchedEmail:
    id: str
    thread_id: str
    sender: str
    subject: str
    date: str
    snippet: str
    body_text: str
    body_html: str


def _load_credentials() -> Credentials:
    creds: Credentials | None = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
        return creds

    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"Missing {CREDENTIALS_PATH.name}. Download OAuth client JSON from "
            "Google Cloud Console and save it at the project root."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json())
    return creds


def get_service():
    creds = _load_credentials()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _decode_part(data: str | None) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _extract_bodies(payload: dict) -> tuple[str, str]:
    """Walk MIME tree and return (text, html)."""
    text_parts: list[str] = []
    html_parts: list[str] = []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        data = body.get("data")
        if mime == "text/plain" and data:
            text_parts.append(_decode_part(data))
        elif mime == "text/html" and data:
            html_parts.append(_decode_part(data))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    return "\n".join(text_parts), "\n".join(html_parts)


def _header(headers: list[dict], name: str) -> str:
    target = name.lower()
    for h in headers:
        if h.get("name", "").lower() == target:
            return h.get("value", "")
    return ""


def search_messages(
    query: str,
    *,
    max_results: int = 25,
    user_id: str = "me",
) -> list[str]:
    """Return message IDs matching a Gmail search query.

    Examples:
        from:jobs-noreply@linkedin.com newer_than:7d
        from:linkedin.com subject:(jobs OR job alerts) newer_than:3d
    """
    service = get_service()
    ids: list[str] = []
    page_token: str | None = None
    try:
        while len(ids) < max_results:
            resp = (
                service.users()
                .messages()
                .list(
                    userId=user_id,
                    q=query,
                    maxResults=min(100, max_results - len(ids)),
                    pageToken=page_token,
                )
                .execute()
            )
            for m in resp.get("messages", []) or []:
                ids.append(m["id"])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except HttpError as e:
        raise RuntimeError(f"Gmail search failed: {e}") from e
    return ids[:max_results]


def fetch_message(message_id: str, *, user_id: str = "me") -> FetchedEmail:
    service = get_service()
    msg = (
        service.users()
        .messages()
        .get(userId=user_id, id=message_id, format="full")
        .execute()
    )
    payload = msg.get("payload", {}) or {}
    headers = payload.get("headers", []) or []
    body_text, body_html = _extract_bodies(payload)
    return FetchedEmail(
        id=msg["id"],
        thread_id=msg.get("threadId", ""),
        sender=_header(headers, "From"),
        subject=_header(headers, "Subject"),
        date=_header(headers, "Date"),
        snippet=msg.get("snippet", ""),
        body_text=body_text,
        body_html=body_html,
    )


def fetch_messages(query: str, *, max_results: int = 25) -> Iterable[FetchedEmail]:
    for mid in search_messages(query, max_results=max_results):
        yield fetch_message(mid)


def send_email(
    *,
    to: str,
    subject: str,
    body: str,
    attachments: list[Path] | None = None,
    user_id: str = "me",
) -> str:
    """Send an email (with optional PDF attachments). Returns Gmail message id."""
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg["From"] = os.environ.get("USER_EMAIL", "me")
    msg.set_content(body)

    for path in attachments or []:
        data = Path(path).read_bytes()
        msg.add_attachment(
            data,
            maintype="application",
            subtype="pdf",
            filename=Path(path).name,
        )

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service = get_service()
    sent = (
        service.users()
        .messages()
        .send(userId=user_id, body={"raw": raw})
        .execute()
    )
    return sent["id"]


if __name__ == "__main__":
    # Smoke test: trigger OAuth and print the user's profile.
    svc = get_service()
    profile = svc.users().getProfile(userId="me").execute()
    print("Authorized as:", profile.get("emailAddress"))
    print("Total messages in mailbox:", profile.get("messagesTotal"))
