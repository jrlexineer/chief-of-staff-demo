"""test_validate.py — the smoke test for the gate's exits.

Six cases below: happy path, null field, no folder match, ambiguous match, and
ungrounded field twice (the second stated as the garbage-scan case it replaces).
PARSE_ERROR is still uncovered — queued as N6. UNKNOWN_DOC_TYPE is covered in
test_policy.py; audit coupling in test_audit_coupling.py.

_route_document is a pure function of (extraction, ground truth, policy), which is
the whole reason the gates live there and not inside an adapter. No mocks needed.
It is private because production callers must go through process_document(), which
audits; tests call it directly to exercise the gate in isolation.

    python -m pytest tests/
"""
from app.models import ExtractedDoc, Route, FlagReason
from app.pipeline.validate import _route_document as route_document
from app.policy_loader import load_policy

POLICY = load_policy()

# Ground truth: the Drive folder tree under 'Active Transactions'.
FOLDERS = {
    "12 Oak St, Springfield": "folder_oak",
    "440 Pine Ave, Springfield": "folder_pine",
}

# A believable text layer for the Oak St contract.
OAK_TEXT = """PURCHASE AND SALE AGREEMENT
Property: 12 Oak St, Springfield
Buyer: Dana Whitfield
Dated this 4th day of March, 2026."""


def _doc(**overrides) -> ExtractedDoc:
    base = dict(
        source_message_id="msg_1",
        original_filename="scan.pdf",
        source_text=OAK_TEXT,
        doc_type="contract",
        property_address="12 Oak St, Springfield",
        client_name="Dana Whitfield",
        doc_date="2026-03-04",
    )
    return ExtractedDoc(**(base | overrides))


def test_happy_path_files():
    """Everything grounded, everything present, exactly one folder → FILE."""
    d = route_document(_doc(), FOLDERS, POLICY)
    assert d.route == Route.FILE
    assert d.target_folder_id == "folder_oak"
    assert d.canonical_name == "contract_12OakStSpringfield_DanaWhitfield.pdf"
    assert "grounding" in d.evidence.gates_passed


def test_null_required_field_flags():
    """A null is the extractor behaving correctly — and it still flags."""
    d = route_document(_doc(client_name=None), FOLDERS, POLICY)
    assert d.route == Route.FLAG
    assert d.flag_reason == FlagReason.NULL_FIELD
    assert "client_name" in d.evidence.detail


def test_no_matching_folder_flags():
    """Address is real and grounded, but no transaction folder exists for it."""
    text = OAK_TEXT.replace("12 Oak St, Springfield", "77 Elm Rd, Springfield")
    d = route_document(
        _doc(property_address="77 Elm Rd, Springfield", source_text=text),
        FOLDERS, POLICY,
    )
    assert d.route == Route.FLAG
    assert d.flag_reason == FlagReason.NO_MATCHING_FOLDER


def test_ambiguous_match_flags():
    """Two folders normalize to the same address → refuse to pick. Never guess."""
    folders = FOLDERS | {"12 Oak St., Springfield": "folder_oak_duplicate"}
    d = route_document(_doc(), folders, POLICY)
    assert d.route == Route.FLAG
    assert d.flag_reason == FlagReason.AMBIGUOUS_MATCH


def test_ungrounded_field_flags():
    """The model returned an address that is nowhere in the document's text layer.
    This is the gate that a garbage scan trips on its own: no text layer, no
    substring match, no filing.
    """
    d = route_document(
        _doc(property_address="440 Pine Ave, Springfield"),  # real folder, wrong doc
        FOLDERS, POLICY,
    )
    assert d.route == Route.FLAG
    assert d.flag_reason == FlagReason.UNGROUNDED_FIELD
    assert "property_address" in d.evidence.detail


def test_garbage_text_layer_flags_without_a_detector():
    """Same gate, stated as the OCR case it replaces."""
    d = route_document(_doc(source_text="~~ ### ~~ illegible ~~"), FOLDERS, POLICY)
    assert d.route == Route.FLAG
    assert d.flag_reason == FlagReason.UNGROUNDED_FIELD
