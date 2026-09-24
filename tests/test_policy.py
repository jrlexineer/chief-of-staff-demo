"""test_policy.py — Policy is immutable and self-attesting.

Covers audit findings L4.1 (Policy is a mutable Pydantic model), L4.2 (policy_hash
is a stored field, so mutating a rule does not change it) and L3.1 (the grounding
gate was switchable off from config).

    python -m pytest tests/test_policy.py
"""
import hashlib
import json

import pytest
import yaml
from pydantic import ValidationError

from app.models import ExtractedDoc, FlagReason, Route
from app.pipeline.validate import _route_document as route_document
from app.policy_loader import POLICY_PATH, Policy, load_policy

POLICY = load_policy()


# --- L4.1: frozen ----------------------------------------------------------

@pytest.mark.parametrize(
    "field, value",
    [
        ("version", 99),
        ("known_doc_types", ["lease"]),
        ("required_fields", {"lease": ["doc_type"]}),
        ("triage_categories", ["whatever"]),
        ("waiting_threshold_days", 999),
    ],
)
def test_policy_fields_cannot_be_assigned(field, value):
    """Every field on Policy refuses in-process mutation."""
    with pytest.raises(ValidationError):
        setattr(POLICY, field, value)


def test_policy_has_no_grounding_toggle():
    """L3.1: there is no config path — field or YAML key — that disables grounding."""
    assert "grounding_required" not in Policy.model_fields
    raw = yaml.safe_load(POLICY_PATH.read_bytes())
    assert "grounding_required" not in raw.get("routing", {})


# --- L4.2: the hash tracks the rules --------------------------------------

def test_policy_hash_is_derived_not_stored():
    assert "policy_hash" not in Policy.model_fields
    expected = hashlib.sha256(
        json.dumps(POLICY.model_dump(), sort_keys=True, default=str).encode()
    ).hexdigest()[:12]
    assert POLICY.policy_hash == expected
    assert len(POLICY.policy_hash) == 12


def test_changed_rule_changes_the_hash():
    """A copy with a different ruleset attests to a different hash. The audit row
    can no longer claim a policy that was not the one applied."""
    mutated = POLICY.model_copy(
        update={"required_fields": POLICY.required_fields | {"contract": ["doc_type"]}}
    )
    assert mutated.policy_hash != POLICY.policy_hash


def test_identical_policies_hash_identically():
    assert load_policy().policy_hash == POLICY.policy_hash


# --- L3.1: the grounding gate is unconditional ----------------------------

def _ungrounded_doc() -> ExtractedDoc:
    return ExtractedDoc(
        source_message_id="msg_1",
        original_filename="scan.pdf",
        source_text="totally unrelated text",
        doc_type="contract",
        property_address="12 Oak St, Springfield",
        client_name="Dana Whitfield",
    )


def test_grounding_gate_cannot_be_switched_off():
    """The audit's reproduction: with grounding off, an ungrounded document filed
    to folder_oak. There is now no policy value that produces that outcome."""
    folders = {"12 Oak St, Springfield": "folder_oak"}
    d = route_document(_ungrounded_doc(), folders, POLICY)
    assert d.route == Route.FLAG
    assert d.flag_reason == FlagReason.UNGROUNDED_FIELD


# --- L3.2: an unlisted doc_type flags, it does not crash -------------------

def test_doc_type_missing_from_required_fields_flags():
    """The audit's reproduction: `KeyError 'lease' -> no flag, no audit row`.

    A Policy whose known_doc_types lists a type that required_fields does not
    cover can no longer be built through load_policy (see the test below), but it
    can still be constructed by hand. The gate must flag rather than raise, because
    the raise happened upstream of store.record_decision — fail-open into silence.
    """
    skewed = POLICY.model_copy(
        update={"known_doc_types": POLICY.known_doc_types + ["lease"]}
    )
    doc = ExtractedDoc(
        source_message_id="msg_1",
        original_filename="scan.pdf",
        source_text="RESIDENTIAL LEASE\nProperty: 12 Oak St, Springfield\nTenant: Dana Whitfield",
        doc_type="lease",
        property_address="12 Oak St, Springfield",
        client_name="Dana Whitfield",
    )
    d = route_document(doc, {"12 Oak St, Springfield": "folder_oak"}, skewed)
    assert d.route == Route.FLAG
    assert d.flag_reason == FlagReason.UNKNOWN_DOC_TYPE
    assert "lease" in d.evidence.detail


def test_policy_file_with_uncovered_doc_type_fails_to_load(tmp_path, monkeypatch):
    """A one-line policy.yaml edit that adds a doc type without adding its
    required_fields entry must fail at boot, not at the first document."""
    raw = yaml.safe_load(POLICY_PATH.read_bytes())
    raw["documents"]["known_doc_types"] = raw["documents"]["known_doc_types"] + ["lease"]
    bad = tmp_path / "policy.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")

    monkeypatch.setattr("app.policy_loader.POLICY_PATH", bad)
    with pytest.raises(ValueError, match="lease"):
        load_policy()


def test_policy_file_with_orphan_required_fields_fails_to_load(tmp_path, monkeypatch):
    """The mismatch is checked in both directions."""
    raw = yaml.safe_load(POLICY_PATH.read_bytes())
    raw["documents"]["required_fields"]["lease"] = ["doc_type"]
    bad = tmp_path / "policy.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")

    monkeypatch.setattr("app.policy_loader.POLICY_PATH", bad)
    with pytest.raises(ValueError, match="lease"):
        load_policy()
