"""test_run_once.py — the wiring, with Gmail and Anthropic mocked.

`run_once` is the first thing in this project that reaches `validate.process_document`
from outside a test (finding B.8: both pipelines were unreachable from `main.py`). It
is glue, so what is worth testing is the glue's decisions:

  - message selection prefers a PDF and says so when it falls back to a body;
  - known_folders keys are refused, not silently normalized (the L3.3 / A.6 contract
    at the one place a human types the dict);
  - a full run writes exactly one audit row, and the row matches the decision.

The end-to-end test drives the real gate and the real store against a temp database,
with only the two network adapters faked. That is the point: nothing about routing,
redaction or the audit write is stubbed out.

    python -m pytest tests/test_run_once.py
"""
import json

import pytest

from app import run_once, store
from app.adapters import gmail
from app.models import ExtractedDoc, Route

pytest_plugins = []


# --------------------------------------------------------------------------
# known_folders
# --------------------------------------------------------------------------

def test_loads_a_normalized_folder_map(tmp_path):
    path = tmp_path / "known_folders.json"
    path.write_text(json.dumps({"12 oak st springfield": "folder_oak"}))
    assert run_once.load_known_folders(path) == {"12 oak st springfield": "folder_oak"}


def test_refuses_a_display_name_key_and_prints_the_normalized_form(tmp_path):
    """The audit's L3.3 scenario, caught where a human types it rather than 100
    documents later as NO_MATCHING_FOLDER on everything."""
    path = tmp_path / "known_folders.json"
    path.write_text(json.dumps({"12 Oak St., Springfield": "folder_oak"}))
    with pytest.raises(ValueError) as exc:
        run_once.load_known_folders(path)
    assert "12 oak st springfield" in str(exc.value)


def test_refuses_an_empty_map(tmp_path):
    path = tmp_path / "known_folders.json"
    path.write_text("{}")
    with pytest.raises(ValueError):
        run_once.load_known_folders(path)


def test_a_missing_file_points_at_the_setup_doc(tmp_path):
    with pytest.raises(FileNotFoundError) as exc:
        run_once.load_known_folders(tmp_path / "nope.json")
    assert "SETUP_LIVE.md" in str(exc.value)


# --------------------------------------------------------------------------
# Message selection
# --------------------------------------------------------------------------

def _msg(mid, attachments=(), body="hello"):
    return gmail.Message(
        id=mid, thread_id="t" + mid, snippet="", headers={"subject": "s"},
        body_text=body, body_source="text/plain", attachments=list(attachments),
    )


PDF = gmail.Attachment("addendum.pdf", "application/pdf", "att-1", 2048)
PNG = gmail.Attachment("sig.png", "image/png", "att-2", 900)


class FakeGmail:
    def __init__(self, messages):
        self._messages = {m.id: m for m in messages}
        self._order = [m.id for m in messages]

    def list_recent_messages(self, service, max_results=10, query=None):
        return self._order[:max_results]

    def fetch_message(self, service, message_id):
        return self._messages[message_id]


@pytest.fixture
def fake_gmail(monkeypatch):
    def install(messages):
        fake = FakeGmail(messages)
        monkeypatch.setattr(gmail, "list_recent_messages", fake.list_recent_messages)
        monkeypatch.setattr(gmail, "fetch_message", fake.fetch_message)
        return fake
    return install


def test_picks_the_newest_message_carrying_a_pdf(fake_gmail):
    fake_gmail([_msg("a", [PNG]), _msg("b", [PDF]), _msg("c", [PDF])])
    message, attachment, note = run_once.pick_message(None, 10, None)
    assert message.id == "b"          # newest-first ordering; "b" is the first with a PDF
    assert attachment == PDF
    assert note == ""


def test_falls_back_to_the_newest_message_body_and_says_so(fake_gmail):
    fake_gmail([_msg("a", [PNG]), _msg("b")])
    message, attachment, note = run_once.pick_message(None, 10, None)
    assert message.id == "a"
    assert attachment is None
    assert "falling back" in note


def test_an_empty_result_is_reported_not_crashed(fake_gmail):
    fake_gmail([])
    message, attachment, note = run_once.pick_message(None, 10, None)
    assert message is None and attachment is None and note


# --------------------------------------------------------------------------
# End to end, with only the network faked
# --------------------------------------------------------------------------

EXTRACTABLE = (
    "ADDENDUM TO PURCHASE AGREEMENT\n"
    "Property: 12 Oak St, Springfield\n"
    "Buyer: Dana Whitfield\n"
    "Dated: March 3, 2026\n"
)


@pytest.fixture
def live_run(tmp_path, monkeypatch, fake_gmail):
    """Everything wired except Gmail transport and the Anthropic call."""
    folders = tmp_path / "known_folders.json"
    folders.write_text(json.dumps({"12 oak st springfield": "folder_oak"}))
    db = tmp_path / "audit.sqlite"

    fake_gmail([_msg("m1", [PDF])])
    monkeypatch.setattr(gmail, "build_client", lambda **kw: "SERVICE")
    monkeypatch.setattr(gmail, "fetch_attachment_bytes", lambda s, mid, att: b"%PDF fake")

    def install_extraction(**fields):
        async def fake(pdf_bytes, message_id, filename, **kw):
            from app.adapters.llm import Extraction, span_check
            doc = ExtractedDoc(
                source_message_id=message_id, original_filename=filename,
                source_text=EXTRACTABLE, **fields,
            )
            return Extraction(doc=doc, span_check=span_check(doc))
        monkeypatch.setattr("app.adapters.llm.extract_document_verbose", fake)

    return {
        "argv": ["--folders", str(folders), "--db", str(db)],
        "db": db,
        "install_extraction": install_extraction,
    }


def _rows(db):
    import sqlite3
    con = sqlite3.connect(db)
    rows = con.execute("SELECT id, route, flag_reason, payload FROM audit ORDER BY id").fetchall()
    con.close()
    return rows


def test_a_clean_document_routes_to_file_and_writes_one_audit_row(live_run, capsys):
    live_run["install_extraction"](
        doc_type="addendum",
        property_address="12 Oak St, Springfield",
        client_name="Dana Whitfield",
        doc_date="March 3, 2026",
    )
    assert run_once.main(live_run["argv"]) == 0

    out = capsys.readouterr().out
    assert "route        FILE" in out
    assert "WOULD-FILE" in out
    assert "folder_oak" in out
    assert "Nothing was uploaded" in out

    rows = _rows(live_run["db"])
    assert len(rows) == 1
    assert rows[0][1] == Route.FILE.value


def test_the_persisted_row_never_contains_the_document_text(live_run):
    """L5.1, end to end rather than at the unit. The text goes through the real
    adapter, the real gate and the real writer, and comes out as a hash."""
    live_run["install_extraction"](
        doc_type="addendum",
        property_address="12 Oak St, Springfield",
        client_name="Dana Whitfield",
        doc_date="March 3, 2026",
    )
    run_once.main(live_run["argv"])

    payload = json.loads(_rows(live_run["db"])[0][3])
    assert "source_text" not in payload["doc"]
    assert "Dana Whitfield" not in payload["doc"]["source_text_sha256"]
    assert payload["doc"]["source_text_chars"] == len(EXTRACTABLE)


def test_a_hallucinated_field_flags_as_ungrounded_not_as_missing(live_run, capsys):
    """The property the adapter's report-don't-repair rule exists to protect: a value
    the model invented reaches the gate intact and is flagged as what it is."""
    live_run["install_extraction"](
        doc_type="addendum",
        property_address="12 Oak St, Springfield",
        client_name="Marguerite Hartley",          # nowhere in EXTRACTABLE
        doc_date="March 3, 2026",
    )
    assert run_once.main(live_run["argv"]) == 0

    out = capsys.readouterr().out
    assert "route        FLAG" in out
    assert "ungrounded_field" in out
    assert "client_name        ungrounded" in out

    rows = _rows(live_run["db"])
    assert rows[0][2] == "ungrounded_field"


def test_an_unresolvable_address_flags_and_files_nothing(live_run, capsys):
    live_run["install_extraction"](
        doc_type="addendum",
        property_address="12 Oak St, Springfield",
        client_name="Dana Whitfield",
        doc_date="March 3, 2026",
    )
    # Same extraction, but the folder map does not know the address.
    folders = live_run["argv"][1]
    from pathlib import Path
    Path(folders).write_text(json.dumps({"5 elm rd shelby": "folder_elm"}))

    assert run_once.main(live_run["argv"]) == 0
    out = capsys.readouterr().out
    assert "no_matching_folder" in out
    assert "FLAGGED for human review" in out
    assert "WOULD-FILE   " not in out          # the action line, not the banner's mention


def test_a_scanned_pdf_flags_as_a_parse_error_rather_than_being_lost(live_run, capsys):
    """PARSE_ERROR had no test at all (finding A.8's remainder, queued as N7). This is
    not that test — it exercises the route, not the gate in isolation — but the path is
    no longer unexercised."""
    live_run["install_extraction"](parse_error="PDF has no usable text layer — a scan")
    assert run_once.main(live_run["argv"]) == 0

    out = capsys.readouterr().out
    assert "route        FLAG" in out
    assert "parse_error" in out
    assert len(_rows(live_run["db"])) == 1


def test_a_refused_folder_map_stops_before_gmail_is_touched(tmp_path, monkeypatch, capsys):
    """Fail closed and fail early: no OAuth prompt for a run that cannot route."""
    def _no_auth(**kw):
        raise AssertionError("run_once authenticated despite an unusable folder map")

    monkeypatch.setattr(gmail, "build_client", _no_auth)
    folders = tmp_path / "known_folders.json"
    folders.write_text(json.dumps({"12 Oak St., Springfield": "folder_oak"}))

    assert run_once.main(["--folders", str(folders), "--db", str(tmp_path / "a.sqlite")]) == 2
    assert "REFUSED" in capsys.readouterr().out
