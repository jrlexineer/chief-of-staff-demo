"""test_gmail_adapter.py — the scope assertion, and the message parser.

Two things are under test, and only the first one is a law.

**The law (L1, L6, D1/D4).** `gmail.build_client()` must call
`app.scopes.assert_safe()` against the scopes the *token actually came back with*,
and must refuse to construct a Google client when they are anything other than
exactly `gmail.readonly`. The tests below drive that with a fake OAuth flow, so the
refusal is exercised for a token Google granted `gmail.send`, `gmail.compose`, an
unexpected extra, and a too-narrow set — without any network or browser.

The subtle one is `test_refuses_a_cached_token_whose_scopes_were_widened`: a token
file on disk is a credential this process did not watch being granted. Re-asserting on
load is what stops a hand-edited or previously over-granted `token.json` from becoming
a send-capable client on the next run.

**The parser (not a law).** Gmail's `users.messages.get` payload is a recursive MIME
tree and getting the walk wrong is how attachments go missing. Exercised against
literal API-shaped dicts.

    python -m pytest tests/test_gmail_adapter.py
"""
import base64
import json

import pytest

from app.adapters import gmail

READONLY = "https://www.googleapis.com/auth/gmail.readonly"
SEND = "https://www.googleapis.com/auth/gmail.send"
COMPOSE = "https://www.googleapis.com/auth/gmail.compose"


# --------------------------------------------------------------------------
# Fakes. Nothing here touches the network, a browser, or a real credential.
# --------------------------------------------------------------------------

class FakeCredentials:
    """Stands in for google.oauth2.credentials.Credentials."""

    def __init__(self, granted, *, valid=True, expired=False, refresh_token="r"):
        self.granted_scopes = list(granted)
        self.scopes = list(granted)
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.token = "tok"

    def to_json(self):
        return json.dumps({
            "token": self.token,
            "refresh_token": self.refresh_token,
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "cid",
            "client_secret": "csecret",
            "scopes": list(self.scopes),
        })

    def refresh(self, request):          # pragma: no cover — not reached in these tests
        self.expired = False
        self.valid = True


class FakeFlow:
    def __init__(self, granted):
        self._granted = granted
        self.credentials = None

    def run_local_server(self, **kwargs):
        self.credentials = FakeCredentials(self._granted)
        return self.credentials


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Patch out the three google entry points and record whether build() was reached.

    `built` staying empty after a RuntimeError is the assertion that matters: the
    refusal has to happen *before* a client exists, not after one is handed back.
    """
    built = []
    monkeypatch.setattr(gmail, "_build_service", lambda creds: built.append(creds) or "SERVICE")

    creds_file = tmp_path / "credentials.json"
    creds_file.write_text('{"installed": {"client_id": "x", "client_secret": "y"}}')
    return {"built": built, "creds_file": creds_file, "token_file": tmp_path / "token.json"}


def _grant(monkeypatch, granted):
    monkeypatch.setattr(gmail, "_new_flow", lambda path, scopes: FakeFlow(granted))


# --------------------------------------------------------------------------
# The law
# --------------------------------------------------------------------------

def test_builds_a_client_when_google_grants_exactly_readonly(wired, monkeypatch):
    _grant(monkeypatch, [READONLY])
    service = gmail.build_client(
        credentials_path=wired["creds_file"], token_path=wired["token_file"]
    )
    assert service == "SERVICE"
    assert len(wired["built"]) == 1


@pytest.mark.parametrize("granted", [
    [READONLY, SEND],
    [READONLY, COMPOSE],
    [READONLY, "https://www.googleapis.com/auth/gmail.modify"],
    [READONLY, "https://mail.google.com/"],
    [READONLY, "https://www.googleapis.com/auth/drive.file"],
    [READONLY, "openid", "https://www.googleapis.com/auth/userinfo.email"],
    [SEND],
    [],
])
def test_refuses_to_construct_a_client_for_anything_but_readonly(wired, monkeypatch, granted):
    """Not a blocklist. A grant that is wider, narrower or merely unexpected all fail
    closed, and the client is never constructed."""
    _grant(monkeypatch, granted)
    with pytest.raises(RuntimeError):
        gmail.build_client(
            credentials_path=wired["creds_file"], token_path=wired["token_file"]
        )
    assert wired["built"] == []


def test_a_rejected_grant_is_not_cached(wired, monkeypatch):
    """A token file written for an over-granted credential would be re-loaded on the
    next run. Refuse first, write second."""
    _grant(monkeypatch, [READONLY, SEND])
    with pytest.raises(RuntimeError):
        gmail.build_client(
            credentials_path=wired["creds_file"], token_path=wired["token_file"]
        )
    assert not wired["token_file"].exists()


def test_an_accepted_grant_caches_the_granted_scopes_not_the_requested_ones(wired, monkeypatch):
    """Credentials.to_json() serializes `scopes` (what was asked for) and drops
    `granted_scopes` (what came back). Writing the requested list would mean the next
    run re-asserts against our own request rather than against Google's answer."""
    _grant(monkeypatch, [READONLY])
    gmail.build_client(credentials_path=wired["creds_file"], token_path=wired["token_file"])
    cached = json.loads(wired["token_file"].read_text())
    assert cached["scopes"] == [READONLY]


def test_reuses_a_cached_token_without_reopening_the_browser(wired, monkeypatch):
    def _no_flow(path, scopes):
        raise AssertionError("build_client ran the OAuth flow despite a valid token")

    monkeypatch.setattr(gmail, "_new_flow", _no_flow)
    monkeypatch.setattr(
        gmail, "_load_cached", lambda path: FakeCredentials([READONLY])
    )
    wired["token_file"].write_text("{}")
    assert gmail.build_client(
        credentials_path=wired["creds_file"], token_path=wired["token_file"]
    ) == "SERVICE"


def test_refuses_a_cached_token_whose_scopes_were_widened(wired, monkeypatch):
    """The token file is a credential this process did not watch being granted."""
    monkeypatch.setattr(gmail, "_load_cached", lambda path: FakeCredentials([READONLY, COMPOSE]))
    wired["token_file"].write_text("{}")
    with pytest.raises(RuntimeError):
        gmail.build_client(
            credentials_path=wired["creds_file"], token_path=wired["token_file"]
        )
    assert wired["built"] == []


def test_requested_scopes_are_read_from_app_scopes(wired, monkeypatch):
    """The scope list is not written down a second time in this adapter — that is how
    a widened request slips past tests/test_scopes.py (finding L6.1)."""
    seen = {}
    monkeypatch.setattr(
        gmail, "_new_flow",
        lambda path, scopes: seen.update(scopes=scopes) or FakeFlow([READONLY]),
    )
    gmail.build_client(credentials_path=wired["creds_file"], token_path=wired["token_file"])

    from app import scopes as app_scopes
    assert set(seen["scopes"]) == set(app_scopes.TRIAGE_SCOPES)


def test_missing_client_secrets_file_says_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setattr(gmail, "_load_cached", lambda path: None)
    with pytest.raises(FileNotFoundError) as exc:
        gmail.build_client(
            credentials_path=tmp_path / "nope.json", token_path=tmp_path / "token.json"
        )
    assert "SETUP_LIVE.md" in str(exc.value)


# --------------------------------------------------------------------------
# The MIME parser
# --------------------------------------------------------------------------

def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


MULTIPART = {
    "id": "m1",
    "threadId": "t1",
    "snippet": "Signed addendum attached",
    "payload": {
        "mimeType": "multipart/mixed",
        "headers": [
            {"name": "Subject", "value": "12 Oak St — addendum"},
            {"name": "From", "value": "Dana Whitfield <dana@example.com>"},
            {"name": "Date", "value": "Tue, 2 Sep 2026 09:14:00 -0400"},
        ],
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64("Signed copy attached.")}},
                    {"mimeType": "text/html", "body": {"data": _b64("<p>Signed copy attached.</p>")}},
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "addendum.pdf",
                "body": {"attachmentId": "att-1", "size": 4096},
            },
            {
                "mimeType": "image/png",
                "filename": "sig.png",
                "body": {"attachmentId": "att-2", "size": 900},
            },
        ],
    },
}


class FakeService:
    """Mimics the chained builder surface: service.users().messages().get(...).execute()."""

    def __init__(self, message=None, listing=None, attachment=None):
        self._message, self._listing, self._attachment = message, listing, attachment
        self.calls = []

    def users(self):
        return self

    def messages(self):
        return self

    def attachments(self):
        return self

    def list(self, **kw):
        self.calls.append(("list", kw))
        return _Exec(self._listing)

    def get(self, **kw):
        self.calls.append(("get", kw))
        return _Exec(self._attachment if "id" in kw and kw.get("id", "").startswith("att") else self._message)


class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


def test_fetch_message_prefers_the_plain_text_part():
    msg = gmail.fetch_message(FakeService(message=MULTIPART), "m1")
    assert msg.body_text == "Signed copy attached."
    assert msg.body_source == "text/plain"
    assert msg.subject == "12 Oak St — addendum"
    assert msg.sender == "Dana Whitfield <dana@example.com>"


def test_fetch_message_finds_attachments_nested_below_the_top_level():
    msg = gmail.fetch_message(FakeService(message=MULTIPART), "m1")
    assert [(a.filename, a.mime_type) for a in msg.attachments] == [
        ("addendum.pdf", "application/pdf"),
        ("sig.png", "image/png"),
    ]
    assert msg.pdf_attachments() == [msg.attachments[0]]


def test_html_only_body_is_converted_and_says_so():
    """Grounding compares extracted fields against this exact string, so a converted
    body is recorded as converted rather than passed off as the message's own text."""
    html_only = {
        "id": "m2", "threadId": "t2", "snippet": "",
        "payload": {
            "mimeType": "text/html",
            "headers": [{"name": "Subject", "value": "hi"}],
            "body": {"data": _b64("<p>Hello <b>Dana</b></p>")},
        },
    }
    msg = gmail.fetch_message(FakeService(message=html_only), "m2")
    assert msg.body_source == "text/html-converted"
    assert "Hello Dana" in msg.body_text


def test_a_message_with_no_body_part_is_empty_not_an_exception():
    bare = {"id": "m3", "threadId": "t3", "snippet": "", "payload": {"mimeType": "text/plain", "headers": []}}
    msg = gmail.fetch_message(FakeService(message=bare), "m3")
    assert msg.body_text == ""
    assert msg.body_source == "none"


def test_list_recent_messages_returns_ids_and_passes_the_query_through():
    service = FakeService(listing={"messages": [{"id": "a"}, {"id": "b"}]})
    assert gmail.list_recent_messages(service, max_results=2, query="has:attachment") == ["a", "b"]
    assert service.calls[0][1]["q"] == "has:attachment"
    assert service.calls[0][1]["maxResults"] == 2


def test_list_recent_messages_on_an_empty_inbox():
    assert gmail.list_recent_messages(FakeService(listing={})) == []


def test_fetch_attachment_bytes_refuses_something_larger_than_the_cap(monkeypatch):
    """Fail closed on size rather than pulling an arbitrary blob into memory and into
    an LLM request."""
    big = gmail.Attachment(
        filename="huge.pdf", mime_type="application/pdf",
        attachment_id="att-1", size_bytes=gmail.MAX_ATTACHMENT_BYTES + 1,
    )
    with pytest.raises(ValueError) as exc:
        gmail.fetch_attachment_bytes(FakeService(), "m1", big)
    assert "MAX_ATTACHMENT_BYTES" in str(exc.value)


def test_fetch_attachment_bytes_decodes_url_safe_base64():
    payload = b"%PDF-1.4 fake"
    service = FakeService(attachment={"data": base64.urlsafe_b64encode(payload).decode()})
    att = gmail.Attachment("a.pdf", "application/pdf", "att-1", len(payload))
    assert gmail.fetch_attachment_bytes(service, "m1", att) == payload
