"""test_scopes.py — the send law, as a test rather than a docstring.

Covers audit findings L1.1 (the law rested on a false claim about gmail.compose),
L1.3 (`grep -r 'send' app/` prescribed as enforcement) and L6 (no scope
declaration existed anywhere in the code).

    python -m pytest tests/test_scopes.py
"""
import pytest

from app import scopes

READONLY = "https://www.googleapis.com/auth/gmail.readonly"


def test_triage_scopes_is_exactly_readonly():
    """Pins the constant. Widening TRIAGE_SCOPES is now an act that breaks a test
    and needs a deliberate edit here, not a quiet addition at OAuth-client
    construction time inside an adapter."""
    assert scopes.TRIAGE_SCOPES == frozenset({READONLY})
    assert isinstance(scopes.TRIAGE_SCOPES, frozenset)


def test_assert_safe_passes_for_exactly_triage_scopes():
    scopes.assert_safe({READONLY})
    scopes.assert_safe(set(scopes.TRIAGE_SCOPES))


@pytest.mark.parametrize(
    "extra",
    [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://mail.google.com/",
    ],
)
def test_assert_safe_rejects_send_capable_scopes(extra):
    """gmail.compose is in this list on purpose: Google documents it as
    'Create, read, update, and delete drafts. Send messages and drafts.' It is an
    accepted authorization scope for users.messages.send. It grants send."""
    with pytest.raises(RuntimeError):
        scopes.assert_safe({READONLY, extra})


@pytest.mark.parametrize(
    "granted",
    [
        set(),
        {"https://www.googleapis.com/auth/gmail.send"},
        {"https://www.googleapis.com/auth/drive.file"},
    ],
)
def test_assert_safe_rejects_any_other_mismatch(granted):
    """Not a blocklist. Anything that is not exactly TRIAGE_SCOPES fails closed,
    including a set that is too narrow or merely unexpected."""
    with pytest.raises(RuntimeError):
        scopes.assert_safe(granted)


def test_banned_scopes_are_named_and_include_compose():
    assert "https://www.googleapis.com/auth/gmail.compose" in scopes.SEND_CAPABLE_SCOPES
    assert not (scopes.TRIAGE_SCOPES & scopes.SEND_CAPABLE_SCOPES)


#: Every public callable app/adapters/gmail.py is allowed to define. Adding a name
#: here is the reviewed act; adding one to the adapter without touching this list is
#: a test failure. Updated when the stub became the real adapter (LIVE.1) — the
#: assertion moved from "these two stubs" to "this named read-only surface", because
#: an equality check against a stub's contents cannot survive the stub being built,
#: and a test that has to be deleted to ship the feature enforces nothing.
GMAIL_READ_SURFACE = {
    "Attachment",              # value type: one attachment's metadata
    "Message",                 # value type: headers, body, attachment manifest
    "build_client",            # asserts scopes, then constructs the client
    "granted_scopes",          # what the token came back with
    "list_recent_messages",    # read
    "fetch_message",           # read
    "fetch_attachment_bytes",  # read
}


def test_gmail_adapter_exposes_no_write_surface():
    """L1.3's replacement: a capability check, not a token search.

    Two halves. This one pins the adapter's public surface by name, so a
    `create_draft`/`dispatch`/`deliver` appearing in this module is a failing test
    rather than a code review someone might skip. The half that actually carries the
    law is tests/test_gmail_adapter.py, which drives build_client() with a fake OAuth
    grant and asserts no client is constructed when Google returns anything wider
    than gmail.readonly. A name list is a convention; the scope refusal is the
    mechanism.
    """
    from app.adapters import gmail

    defined_here = {
        n for n in dir(gmail)
        if not n.startswith("_")
        and callable(getattr(gmail, n))
        and getattr(getattr(gmail, n), "__module__", None) == gmail.__name__
    }
    assert defined_here == GMAIL_READ_SURFACE
