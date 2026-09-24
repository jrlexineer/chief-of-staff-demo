"""test_corrections.py — corrections append; the audit row is never revised.

Covers audit findings L5.4 (the "append-only" table was updated in place, destroying
pre-correction state and recording no timestamp), L5.5 (`/queue/{id}/resolve`
validated nothing and took `correction` as a query parameter, so human corrections
landed in access logs), A.4 (the docstring/`UPDATE` contradiction) and B.3.

    python -m pytest tests/test_corrections.py
"""
import inspect
import json
import pathlib
import sqlite3

import pytest
from fastapi import HTTPException

from app import main, store
from app.models import ExtractedDoc, Route
from app.pipeline import validate
from app.policy_loader import load_policy

POLICY = load_policy()
FOLDERS = {"12 Oak St, Springfield": "folder_oak"}

OAK_TEXT = """PURCHASE AND SALE AGREEMENT
Property: 12 Oak St, Springfield
Buyer: Dana Whitfield"""


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "audit.sqlite"
    monkeypatch.setattr(store, "DB", path)
    store.init()
    return path


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


def _flagged() -> int:
    """A flagged row, created through the audited path. Returns its audit id."""
    validate.process_document(_doc(client_name=None), FOLDERS, POLICY, store)
    return store.flag_queue()[-1]["id"]


def _filed() -> int:
    validate.process_document(_doc(), FOLDERS, POLICY, store)
    con = sqlite3.connect(store.DB)
    rid = con.execute("SELECT id FROM audit WHERE route='file' ORDER BY id DESC").fetchone()[0]
    con.close()
    return rid


def _row(audit_id: int) -> tuple:
    con = sqlite3.connect(store.DB)
    row = con.execute(
        "SELECT id, ts, route, flag_reason, policy_hash, payload FROM audit WHERE id=?",
        (audit_id,),
    ).fetchone()
    con.close()
    return row


# --- L5.4 / A.4: the table is append-only ----------------------------------

def test_no_update_of_an_audit_row_outside_the_redaction_sweep():
    """The tripwire. `UPDATE audit` may appear in exactly one place — the content
    redaction sweep, which destroys payload text and revises nothing else. Any second
    occurrence means a decision field became editable and this test is the alarm."""
    source = pathlib.Path(store.__file__).read_text(encoding="utf-8")
    sanctioned = inspect.getsource(store.redact_legacy_rows)
    assert source.count("UPDATE audit") == 1
    assert sanctioned.count("UPDATE audit") == 1


def test_resolved_is_not_a_column_on_the_audit_row(db):
    """Derived by join, never stored. A stored flag is a second source of truth that
    can disagree with the corrections table."""
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(audit)")}
    con.close()
    assert "human_correction" not in cols
    assert "resolved" not in cols


def test_a_correction_leaves_every_decision_field_byte_identical(db):
    """The pre-correction state the old UPDATE destroyed."""
    audit_id = _flagged()
    before = _row(audit_id)
    store.add_correction(audit_id, "belongs to the Pine Ave file")
    assert _row(audit_id) == before


# --- L5.5: the endpoint validates -----------------------------------------

def test_resolving_a_flag_appends_and_flips_the_derived_status(db):
    audit_id = _flagged()
    assert [i["id"] for i in store.flag_queue()] == [audit_id]

    result = main.resolve(audit_id, main.Correction(correction="client is Dana Whitfield"))

    assert result["resolved"] == audit_id
    assert store.flag_queue() == []                  # derived, from the join
    (correction,) = store.corrections_for(audit_id)
    assert correction["correction"] == "client is Dana Whitfield"
    assert correction["created_at"]


def test_an_unknown_audit_id_is_404(db):
    with pytest.raises(HTTPException) as exc:
        main.resolve(9999, main.Correction(correction="whatever"))
    assert exc.value.status_code == 404


def test_resolving_a_filed_row_is_409(db):
    """Correcting a document that was never flagged is not a resolution; the old
    endpoint accepted it and silently wrote nothing anyone would look at."""
    audit_id = _filed()
    with pytest.raises(HTTPException) as exc:
        main.resolve(audit_id, main.Correction(correction="wrong folder"))
    assert exc.value.status_code == 409
    assert store.corrections_for(audit_id) == []


def test_the_correction_is_a_body_field_not_a_query_parameter():
    """The sharp part of L5.5: `correction: str` as a bare scalar made it a query
    parameter, so human corrections — the text of a mistake about a real client —
    landed in access logs."""
    params = inspect.signature(main.resolve).parameters
    assert params["body"].annotation is main.Correction
    assert "correction" not in params


def test_an_empty_correction_is_rejected():
    with pytest.raises(ValueError):
        main.Correction(correction="   ")


# --- corrections accumulate ------------------------------------------------

def test_two_corrections_append_in_order(db):
    audit_id = _flagged()
    store.add_correction(audit_id, "first take")
    store.add_correction(audit_id, "actually, second take")

    rows = store.corrections_for(audit_id)
    assert [r["correction"] for r in rows] == ["first take", "actually, second take"]
    assert store.latest_correction(audit_id)["correction"] == "actually, second take"
    assert store.flag_queue() == []


def test_a_correction_on_a_missing_row_raises_at_the_store(db):
    """The endpoint is not the only possible caller — the same lesson as L5.2."""
    with pytest.raises(store.UnknownAuditRow):
        store.add_correction(4242, "x")
    with pytest.raises(store.NotAFlag):
        store.add_correction(_filed(), "x")


def test_legacy_human_corrections_are_carried_over(db):
    """A table written before this change holds corrections in the old column. They
    are the training data the docstring calls irreplaceable; deriving resolution from
    a join they are absent from would quietly un-resolve every one of them."""
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE audit ADD COLUMN human_correction TEXT")
    con.execute(
        "INSERT INTO audit (route, flag_reason, policy_hash, payload, human_correction) "
        "VALUES ('flag','null_field','deadbeef1234',?,?)",
        (json.dumps({"route": "flag", "doc": {}}), "the old resolution"),
    )
    con.commit()
    audit_id = con.execute("SELECT MAX(id) FROM audit").fetchone()[0]
    con.close()

    assert store.migrate_legacy_corrections() == 1
    assert store.latest_correction(audit_id)["correction"] == "the old resolution"
    assert store.migrate_legacy_corrections() == 0          # idempotent
    assert len(store.corrections_for(audit_id)) == 1
