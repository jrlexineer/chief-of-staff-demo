"""test_audit_coupling.py — no routing decision without an audit row.

Covers audit finding L5.2: `route_document` was pure and public, and
`store.record_decision` was a separate voluntary call made in exactly one place, so
any second consumer — a batch re-file, a dark-launch script, a CLI — produced
routing decisions with no audit row. Nothing structural forced the write.

    python -m pytest tests/test_audit_coupling.py
"""
import pytest

from app.models import ExtractedDoc, FlagReason, Route
from app.pipeline import validate
from app.policy_loader import load_policy

POLICY = load_policy()
FOLDERS = {"12 Oak St, Springfield": "folder_oak"}

OAK_TEXT = """PURCHASE AND SALE AGREEMENT
Property: 12 Oak St, Springfield
Buyer: Dana Whitfield"""


class RecordingStore:
    """Stand-in for app.store. record_decision has the real signature."""

    def __init__(self, fail: bool = False):
        self.rows: list[tuple] = []
        self.fail = fail

    def record_decision(self, decision, policy_hash):
        if self.fail:
            raise OSError("disk full")
        self.rows.append((decision, policy_hash))
        return len(self.rows)


def _doc(**overrides) -> ExtractedDoc:
    base = dict(
        source_message_id="msg_1",
        original_filename="scan.pdf",
        source_text=OAK_TEXT,
        doc_type="contract",
        property_address="12 Oak St, Springfield",
        client_name="Dana Whitfield",
    )
    return ExtractedDoc(**(base | overrides))


def test_the_pure_gate_is_private():
    """The only public way to get a routing decision is the audited wrapper."""
    assert not hasattr(validate, "route_document")
    assert hasattr(validate, "_route_document")


def test_file_decision_is_recorded():
    store = RecordingStore()
    d = validate.process_document(_doc(), FOLDERS, POLICY, store)
    assert d.route == Route.FILE
    assert len(store.rows) == 1
    assert store.rows[0] == (d, POLICY.policy_hash)


def test_flag_decision_is_recorded():
    store = RecordingStore()
    d = validate.process_document(_doc(client_name=None), FOLDERS, POLICY, store)
    assert d.route == Route.FLAG
    assert len(store.rows) == 1


def test_exception_during_routing_still_yields_an_audit_row(monkeypatch):
    """The L5.2 requirement. A crash inside the gate must not swallow the document:
    it becomes a FLAG the human can see, and the row is written either way."""

    def boom(*args, **kwargs):
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(validate, "_route_document", boom)

    store = RecordingStore()
    d = validate.process_document(_doc(), FOLDERS, POLICY, store)

    assert d.route == Route.FLAG
    assert d.flag_reason == FlagReason.EXTRACTION_ERROR
    assert "gate exploded" in d.evidence.detail
    assert d.doc.source_message_id == "msg_1"
    assert len(store.rows) == 1
    assert store.rows[0][0] is d


def test_a_failed_audit_write_fails_closed(monkeypatch):
    """If the audit row cannot be written, the caller must not receive a decision to
    act on. Filing a document we cannot account for is the failure mode this whole
    table exists to prevent."""
    store = RecordingStore(fail=True)
    with pytest.raises(OSError):
        validate.process_document(_doc(), FOLDERS, POLICY, store)
    assert store.rows == []
