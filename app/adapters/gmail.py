"""gmail.py — the read adapter. Per-user OAuth, READ-ONLY, by construction.

This adapter may hold exactly the scopes in `app.scopes.TRIAGE_SCOPES`
(gmail.readonly, and nothing else). `build_client()` calls `app.scopes.assert_safe()`
with the scopes the token ACTUALLY CAME BACK WITH — not the list this module asked
for — and it does so *before* a Google client object exists. If Google grants
anything beyond gmail.readonly, the RuntimeError is raised and there is no client to
misuse. See app/scopes.py for D1/D2/D4 and the reason gmail.compose is banned.

There is no draft-creation function here and no send function here, because the
credential this adapter holds cannot perform either operation.

Three properties worth stating outright, because each is a place this could quietly
go wrong:

  1. THE SCOPE LIST IS NOT WRITTEN DOWN HERE. It is read from `app.scopes`. A scope
     added at OAuth-client-construction time inside an adapter is exactly the hole
     finding L6.1 described, and it would not break tests/test_scopes.py.

  2. THE CACHED TOKEN IS RE-ASSERTED ON EVERY LOAD. A token.json on disk is a
     credential this process did not watch being granted — it may predate a
     tightening, or have been hand-edited. Loading it is not evidence about it.

  3. THE TOKEN FILE RECORDS THE GRANTED SCOPES, NOT THE REQUESTED ONES.
     `Credentials.to_json()` serializes `scopes` (our request) and drops
     `granted_scopes` (Google's answer), so a naive `to_json()` would make every
     later run re-assert against our own request. The write is fixed up in
     `_cache_token`.

Everything here is synchronous: googleapiclient is a blocking library, and an
`async def` that never awaits is a lie about what the call does to the event loop.
"""
from __future__ import annotations

import base64
import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.scopes import TRIAGE_SCOPES, assert_safe

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Google Cloud OAuth *desktop* client secrets, downloaded from the console.
#: Gitignored. See SETUP_LIVE.md for how to produce it.
DEFAULT_CREDENTIALS_PATH = _REPO_ROOT / "credentials.json"

#: Where the user's refresh token is cached after the first consent. Gitignored.
DEFAULT_TOKEN_PATH = _REPO_ROOT / "token.json"

#: Upper bound on a single attachment we will pull into memory and hand to an
#: extractor. Fail closed on size rather than discovering the limit as an OOM or an
#: API error halfway through a run.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Attachment:
    filename: str
    mime_type: str
    attachment_id: str
    size_bytes: int

    def is_pdf(self) -> bool:
        return (
            self.mime_type == "application/pdf"
            or self.filename.lower().endswith(".pdf")
        )


@dataclass(frozen=True)
class Message:
    """One Gmail message, flattened.

    `body_source` records WHERE body_text came from. It matters because the routing
    gate checks extracted fields for containment in this exact string: a body we
    derived from HTML is a text layer we manufactured, and a reader of the audit row
    should be able to tell the difference between that and the sender's own words.
    """

    id: str
    thread_id: str
    snippet: str
    headers: dict[str, str]
    body_text: str
    body_source: str                       # "text/plain" | "text/html-converted" | "none"
    attachments: list[Attachment] = field(default_factory=list)

    @property
    def subject(self) -> str:
        return self.headers.get("subject", "")

    @property
    def sender(self) -> str:
        return self.headers.get("from", "")

    @property
    def date(self) -> str:
        return self.headers.get("date", "")

    def pdf_attachments(self) -> list[Attachment]:
        return [a for a in self.attachments if a.is_pdf()]


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _new_flow(credentials_path: Path, scopes: list[str]):
    """Seam. Patched in tests so the scope assertion can be driven without a browser."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    return InstalledAppFlow.from_client_secrets_file(str(credentials_path), scopes)


def _load_cached(token_path: Path):
    """Seam. Returns cached Credentials, or None when there is no usable token file."""
    if not Path(token_path).exists():
        return None
    from google.oauth2.credentials import Credentials

    try:
        # No `scopes=` argument on purpose: passing one makes the object report what
        # we asked for. The file's own `scopes` key is what was granted (see
        # _cache_token), and that is the thing worth asserting against.
        return Credentials.from_authorized_user_file(str(token_path))
    except (ValueError, KeyError, json.JSONDecodeError):
        return None                        # malformed cache → re-consent, never proceed


def _build_service(creds):
    """Seam. Constructs the actual Google client. Never reached before assert_safe."""
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def granted_scopes(creds) -> list[str]:
    """What the token came back with, preferring Google's answer over our request.

    `granted_scopes` is populated from the token endpoint response and is the
    authoritative value. It is absent on a credential rehydrated from disk, where
    `scopes` holds the granted set instead — see _cache_token.
    """
    return sorted(getattr(creds, "granted_scopes", None) or getattr(creds, "scopes", None) or [])


def _cache_token(creds, token_path: Path, granted: list[str]) -> None:
    """Write the refresh token, with `scopes` overwritten to the GRANTED set.

    Called only after assert_safe has passed. A rejected grant is never cached: it
    would be re-loaded on the next run, and the next run's refusal would then look
    like a mystery rather than a repeat of this one.
    """
    data = json.loads(creds.to_json())
    data["scopes"] = sorted(granted)
    Path(token_path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def build_client(credentials_path=None, token_path=None, *, open_browser: bool = True):
    """Authenticate and return a read-only Gmail service.

    Raises RuntimeError — before any client exists — if the granted scopes are not
    exactly `app.scopes.TRIAGE_SCOPES`.
    """
    credentials_path = Path(credentials_path or DEFAULT_CREDENTIALS_PATH)
    token_path = Path(token_path or DEFAULT_TOKEN_PATH)

    creds = _load_cached(token_path)

    if creds is not None and getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
        from google.auth.transport.requests import Request

        creds.refresh(Request())
        assert_safe(granted_scopes(creds))          # a refresh can return a new grant
        _cache_token(creds, token_path, granted_scopes(creds))

    if creds is None or not getattr(creds, "valid", False):
        if not credentials_path.exists():
            raise FileNotFoundError(
                f"No OAuth client secrets at {credentials_path}. Create a Desktop-app "
                "OAuth client in Google Cloud, download the JSON, and save it there. "
                "Step-by-step: SETUP_LIVE.md."
            )
        flow = _new_flow(credentials_path, sorted(TRIAGE_SCOPES))
        try:
            creds = flow.run_local_server(port=0, open_browser=open_browser)
        except Warning as exc:
            # oauthlib raises a bare Warning ("Scope has changed from X to Y") when the
            # grant does not match the request. That is this module's central failure
            # case; it deserves a message that names the law rather than a stray
            # warning class escaping as an exception.
            raise RuntimeError(
                f"Google returned a different scope set than was requested: {exc}. "
                "Refusing to continue. See app/scopes.py (D1, D2, D4)."
            ) from exc

        granted = granted_scopes(creds)
        assert_safe(granted)                        # BEFORE the token is cached...
        _cache_token(creds, token_path, granted)
    else:
        assert_safe(granted_scopes(creds))

    return _build_service(creds)                    # ...and BEFORE a client exists.


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def list_recent_messages(service, max_results: int = 10, query: str | None = None) -> list[str]:
    """Message ids, newest first. `query` is Gmail search syntax, e.g.
    'has:attachment filename:pdf'."""
    response = (
        service.users()
        .messages()
        .list(userId="me", maxResults=max_results, q=query or "")
        .execute()
    )
    return [m["id"] for m in response.get("messages", [])]


def fetch_message(service, message_id: str) -> Message:
    """One message: headers, body text, and the attachment manifest.

    Attachment *bytes* are deliberately not fetched here — the manifest is enough to
    decide which message to process, and pulling every blob to answer that question
    is a lot of bytes for a question that is about metadata.
    """
    raw = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )
    payload = raw.get("payload") or {}
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

    parts = list(_walk(payload))
    body_text, body_source = _best_body(parts)

    return Message(
        id=raw.get("id", message_id),
        thread_id=raw.get("threadId", ""),
        snippet=html.unescape(raw.get("snippet", "")),
        headers=headers,
        body_text=body_text,
        body_source=body_source,
        attachments=_attachments(parts),
    )


def fetch_attachment_bytes(service, message_id: str, attachment: Attachment) -> bytes:
    if attachment.size_bytes > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"'{attachment.filename}' is {attachment.size_bytes} bytes, over "
            f"MAX_ATTACHMENT_BYTES ({MAX_ATTACHMENT_BYTES}). Refusing to fetch it."
        )
    response = (
        service.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment.attachment_id)
        .execute()
    )
    return _decode(response.get("data", ""))


# ---------------------------------------------------------------------------
# MIME walking
# ---------------------------------------------------------------------------

def _walk(part: dict):
    """Depth-first over the MIME tree. Gmail nests text/plain two levels down inside
    multipart/mixed → multipart/alternative, so a single pass over payload['parts']
    finds the alternative container and no body at all."""
    yield part
    for child in part.get("parts") or []:
        yield from _walk(child)


def _decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _part_text(part: dict) -> str:
    data = (part.get("body") or {}).get("data")
    return _decode(data).decode("utf-8", errors="replace") if data else ""


def _best_body(parts: list[dict]) -> tuple[str, str]:
    for part in parts:
        if part.get("mimeType") == "text/plain" and not part.get("filename"):
            text = _part_text(part)
            if text.strip():
                return text, "text/plain"

    for part in parts:
        if part.get("mimeType") == "text/html" and not part.get("filename"):
            text = _part_text(part)
            if text.strip():
                return _html_to_text(text), "text/html-converted"

    return "", "none"


_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(?:p|div|br|tr|li|h[1-6]|table)\b[^>]*>", re.I)
_DROP = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)


def _html_to_text(raw: str) -> str:
    """Minimal, deliberately dumb HTML flattening.

    It is not a renderer and does not try to be. Whatever it produces becomes
    `source_text`, so the routing gate checks the model's claims against this same
    string — the conversion cannot silently un-ground a field, because both sides see
    the identical text. What it can do is make a body ugly, which is why
    `body_source` records that it happened.
    """
    text = _DROP.sub(" ", raw)
    text = _BLOCK.sub("\n", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _attachments(parts: list[dict]) -> list[Attachment]:
    out = []
    for part in parts:
        body = part.get("body") or {}
        if part.get("filename") and body.get("attachmentId"):
            out.append(Attachment(
                filename=part["filename"],
                mime_type=part.get("mimeType", "application/octet-stream"),
                attachment_id=body["attachmentId"],
                size_bytes=int(body.get("size", 0)),
            ))
    return out
