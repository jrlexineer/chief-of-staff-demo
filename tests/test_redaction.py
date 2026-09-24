"""test_redaction.py — the audit table never holds a document's text layer.

Covers audit finding L5.1: `store.record_decision` serialized the whole
`RoutingDecision` with `model_dump_json()`, which embeds `ExtractedDoc.source_text`,
so complete legal-document contents — PII and deal terms included — sat indefinitely
in the audit table. The audit's verified payload was:

    {"route":"flag","doc":{...,"source_text":"CONFIDENTIAL PURCHASE AGREEMENT ...
     SSN 123-45-6789 ... price $1,200,000",...}}

    python -m pytest tests/test_redaction.py
"""
import hashlib
import json
import sqlite3

import pytest

from app import redact, store
from app.models import ExtractedDoc, Evidence, Route, RoutingDecision
from app.pipeline import validate
from app.policy_loader import load_policy

POLICY = load_policy()
FOLDERS = {"12 Oak St, Springfield": "folder_oak"}

# The audit's reproduction document. The secrets sit well past PREVIEW_CHARS on
# purpose: a preview that happened to be short enough to exclude them would prove
# nothing about the cap.
SECRETS = ["123-45-6789", "1,200,000", "Whitfield family trust"]
CONFIDENTIAL_TEXT = (
    "CONFIDENTIAL PURCHASE AND SALE AGREEMENT\n"
    "Property: 12 Oak St, Springfield\n"
    "Buyer: Dana Whitfield\n"
    + "Recitals and standard conditions of sale follow in the usual form. " * 4
    + "\nBuyer SSN 123-45-6789. Purchase price $1,200,000, funded by the "
    "Whitfield family trust.\n"
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A throwaway audit table. Never touches the repo-root data.sqlite."""
    path = tmp_path / "audit.sqlite"
    monkeypatch.setattr(store, "DB", path)
    store.init()
    return path


def _doc(**overrides) -> ExtractedDoc:
    base = dict(
        source_message_id="msg_1",
        original_filename="scan.pdf",
        source_text=CONFIDENTIAL_TEXT,
        doc_type="contract",
        property_address="12 Oak St, Springfield",
        client_name="Dana Whitfield",
    )
    return ExtractedDoc(**(base | overrides))


def _payloads(path) -> list[str]:
    con = sqlite3.connect(path)
    rows = [r[0] for r in con.execute("SELECT payload FROM audit").fetchall()]
    con.close()
    return rows


# --- L5.1: the reproduction ------------------------------------------------

def test_the_stored_payload_does_not_contain_the_document(db):
    """The audit's finding, re-run against the real writer."""
    decision = validate.process_document(_doc(), FOLDERS, POLICY, store)
    assert decision.route == Route.FILE

    raw = _payloads(db)[0]
    assert '"source_text"' not in raw
    for secret in SECRETS:
        assert secret not in raw

    stored = json.loads(raw)["doc"]
    assert stored["source_text_sha256"] == hashlib.sha256(
        CONFIDENTIAL_TEXT.encode("utf-8")
    ).hexdigest()
    assert stored["source_text_chars"] == len(CONFIDENTIAL_TEXT)


def test_a_flagged_document_is_redacted_too(db):
    """Flags are the rows a human actually reads, so they are the tempting place to
    keep 'just a bit more context'."""
    validate.process_document(_doc(client_name=None), FOLDERS, POLICY, store)
    raw = _payloads(db)[0]
    assert json.loads(raw)["route"] == "flag"
    assert '"source_text"' not in raw
    for secret in SECRETS:
        assert secret not in raw


def test_the_flag_queue_serves_the_redacted_payload(db):
    """The review-queue surface reads the same rows; it cannot re-expose the text."""
    validate.process_document(_doc(client_name=None), FOLDERS, POLICY, store)
    (item,) = store.flag_queue()
    assert "source_text" not in item["payload"]["doc"]
    assert item["payload"]["doc"]["source_text_preview"].startswith("CONFIDENTIAL")


# --- the shape is a type, not a habit --------------------------------------

def test_the_audit_shape_has_no_source_text_field():
    """Stripping is not something the writer remembers to do — RedactedDoc has
    nowhere to put a text layer."""
    assert "source_text" not in redact.RedactedDoc.model_fields
    assert "source_text" not in redact.RedactedDecision.model_fields


def test_every_extracted_doc_field_is_accounted_for():
    """A new field on ExtractedDoc must be a deliberate decision — kept in the audit
    row or dropped from it — and not an accident of nobody looking."""
    kept = set(ExtractedDoc.model_fields) - {"source_text"}
    derived = {"source_text_sha256", "source_text_preview", "source_text_chars"}
    assert set(redact.RedactedDoc.model_fields) == kept | derived


def test_every_routing_decision_field_is_accounted_for():
    assert set(redact.RedactedDecision.model_fields) == set(RoutingDecision.model_fields)


# --- what survives redaction -----------------------------------------------

def test_the_hash_identifies_the_document():
    """The row must still prove which document it decided about."""
    a = redact.redact_doc(_doc())
    b = redact.redact_doc(_doc(source_text=CONFIDENTIAL_TEXT + "."))
    assert a.source_text_sha256 != b.source_text_sha256
    assert redact.redact_doc(_doc()).source_text_sha256 == a.source_text_sha256


def test_the_preview_is_capped_and_the_truncation_is_visible():
    r = redact.redact_doc(_doc())
    assert len(r.source_text_preview) == redact.PREVIEW_CHARS
    assert r.source_text_preview == CONFIDENTIAL_TEXT[: redact.PREVIEW_CHARS]
    assert r.source_text_chars > redact.PREVIEW_CHARS


def test_a_short_document_previews_whole():
    r = redact.redact_doc(_doc(source_text="Short memo."))
    assert r.source_text_preview == "Short memo."
    assert r.source_text_chars == 11


def test_an_empty_text_layer_still_hashes():
    """A garbage scan has no text layer. It still gets a row, and the hash of the
    empty string is a real value, not a null."""
    r = redact.redact_doc(_doc(source_text=""))
    assert r.source_text_chars == 0
    assert r.source_text_sha256 == hashlib.sha256(b"").hexdigest()


def test_the_routing_fields_survive():
    """Redaction removes the evidence, not the decision."""
    r = redact.redact_doc(_doc())
    assert r.source_message_id == "msg_1"
    assert r.original_filename == "scan.pdf"
    assert r.doc_type == "contract"
    assert r.property_address == "12 Oak St, Springfield"
    assert r.client_name == "Dana Whitfield"


def test_redacting_a_decision_keeps_the_decision():
    decision = validate._route_document(_doc(), FOLDERS, POLICY)
    r = redact.redact(decision)
    assert r.route == decision.route
    assert r.target_folder_id == decision.target_folder_id
    assert r.canonical_name == decision.canonical_name
    assert r.evidence == decision.evidence


# --- rows written before this module existed -------------------------------

def test_a_legacy_row_is_redacted_at_init(db):
    """CREATE TABLE IF NOT EXISTS means an existing table survives the upgrade with
    its unredacted rows intact. Redacting new writes only would leave the exposure
    the finding is actually about — text at rest."""
    legacy = RoutingDecision(
        route=Route.FLAG, doc=_doc(), evidence=Evidence(detail="legacy")
    ).model_dump_json()
    assert "123-45-6789" in legacy          # the pre-fix payload, verbatim

    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO audit (route, flag_reason, policy_hash, payload) VALUES (?,?,?,?)",
        ("flag", "null_field", "deadbeef1234", legacy),
    )
    con.commit()
    con.close()

    store.init()

    raw = _payloads(db)[0]
    assert '"source_text"' not in raw
    for secret in SECRETS:
        assert secret not in raw
    stored = json.loads(raw)["doc"]
    assert stored["source_text_sha256"] == hashlib.sha256(
        CONFIDENTIAL_TEXT.encode("utf-8")
    ).hexdigest()
    assert stored["source_text_preview"] == CONFIDENTIAL_TEXT[: redact.PREVIEW_CHARS]


def test_an_exception_message_cannot_smuggle_the_document_into_a_row(db, monkeypatch):
    """The EXTRACTION_ERROR path interpolates an arbitrary exception message into
    evidence.detail. A parser that quotes the text it choked on would put the
    document back into the row through the one field redaction does not cover."""

    def boom(*args, **kwargs):
        raise RuntimeError(f"pdf text extraction failed near: {CONFIDENTIAL_TEXT}")

    monkeypatch.setattr(validate, "_route_document", boom)

    decision = validate.process_document(_doc(), FOLDERS, POLICY, store)
    assert decision.flag_reason.value == "extraction_error"

    raw = _payloads(db)[0]
    for secret in SECRETS:
        assert secret not in raw
    assert CONFIDENTIAL_TEXT not in raw
    assert "RuntimeError" in raw                     # the row still says what broke


def test_the_exception_detail_is_bounded(db, monkeypatch):
    """Bounded by the same budget as the preview, in the same place, so 'how much
    document text may a row hold' has one answer and not two."""

    def boom(*args, **kwargs):
        raise RuntimeError("x" * 5000)

    monkeypatch.setattr(validate, "_route_document", boom)

    decision = validate.process_document(_doc(), FOLDERS, POLICY, store)
    assert "x" * redact.PREVIEW_CHARS in decision.evidence.detail
    assert "x" * (redact.PREVIEW_CHARS + 1) not in decision.evidence.detail


def test_the_sweep_leaves_redacted_rows_alone(db):
    validate.process_document(_doc(), FOLDERS, POLICY, store)
    before = _payloads(db)
    assert store.redact_legacy_rows() == 0
    assert _payloads(db) == before
