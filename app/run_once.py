"""run_once.py — one real email, end to end, through the real gates.

    python -m app.run_once
    python -m app.run_once --query "has:attachment filename:pdf" --scan 25

WHAT IT DOES

  1. Builds the policy and the database through `main.create_app()` — the
     composition root (P2.12), so this CLI is not a second place that constructs a
     Policy.
  2. Loads a known_folders dict from disk (`live/known_folders.json`). This stands in
     for `drive.list_transaction_folders()`, which is still a stub.
  3. Authenticates to Gmail READ-ONLY. `gmail.build_client()` refuses to construct a
     client if Google grants anything beyond gmail.readonly.
  4. Finds the most recent message carrying a PDF attachment. If none is found in the
     scanned window, it falls back to the latest message and extracts from the BODY
     text, saying loudly that it did so.
  5. Extracts with claude-sonnet-4-6 (app/adapters/llm.py).
  6. Routes through `validate.process_document()` — the real gates, the real policy —
     which writes the audit row as part of producing the decision.
  7. Prints the decision, the extraction's own uncertainty notes, the grounding
     check, and the redacted audit payload that was persisted.

WHAT IT CANNOT DO

  - It cannot send. The credential is gmail.readonly (D1/D4, app/scopes.py).
  - It cannot write to Drive. No Drive credential exists in this project and
    `app/adapters/drive.py` is still a stub, so a FILE decision prints a WOULD-FILE
    line naming the folder id and canonical filename. Nothing is uploaded, moved,
    labelled, archived or modified anywhere.

The only thing this program writes is a row in the local sqlite audit table and text
on stdout.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from app import store
from app.adapters import gmail, llm
from app.main import create_app
from app.models import Route
from app.pipeline.validate import process_document, _normalize
from app.redact import redact

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FOLDERS_PATH = _REPO_ROOT / "live" / "known_folders.json"

BANNER = """\
================================================================================
 CHIEF OF STAFF - single live run
 READ-ONLY. This process holds gmail.readonly and cannot send or draft mail.
 No Drive credential exists: a FILE decision prints WOULD-FILE and writes nothing.
================================================================================"""


def _utf8_stdout() -> None:
    """Windows consoles default to cp1252, which raises UnicodeEncodeError on a
    character a real email subject or a real address is quite likely to contain. A run
    that dies while printing a decision it already recorded is the worst possible
    failure here — the audit row exists and the human never sees it."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------

def load_known_folders(path: Path) -> dict[str, str]:
    """Read {normalized address: drive folder id} and refuse anything else.

    drive.py's contract (findings L3.3 / A.6) says the keys are NORMALIZED addresses.
    This file is the one place a human hand-writes that dict, so it is the one place
    the contract can be broken by typing. Silently normalizing here would be the same
    drift the contract exists to prevent — two normalizations that agree until they
    don't — so a non-normalized key is refused, with the correct form printed.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"No known_folders file at {path}. Copy live/known_folders.example.json "
            "to that path and put your own transaction folders in it. See SETUP_LIVE.md."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data:
        raise ValueError(f"{path} must be a non-empty JSON object of address -> folder id")

    bad = {k: _normalize(k) for k in data if _normalize(k) != k}
    if bad:
        lines = "\n".join(f"    {k!r}  ->  {v!r}" for k, v in bad.items())
        raise ValueError(
            f"{path}: keys must already be normalized addresses (lowercase, no commas "
            f"or periods, single-spaced). Rewrite these keys as:\n{lines}"
        )
    return {str(k): str(v) for k, v in data.items()}


# ---------------------------------------------------------------------------
# Choosing a message
# ---------------------------------------------------------------------------

def pick_message(service, scan: int, query: str | None):
    """(message, attachment_or_None, note). Newest message with a PDF wins.

    Falls back to the newest message overall when the scanned window has no PDF —
    with a note, because extracting filing fields from an email body is a different
    thing than extracting them from a document, and the run should say which happened.
    """
    ids = gmail.list_recent_messages(service, max_results=scan, query=query)
    if not ids:
        return None, None, "no messages matched"

    first = None
    for message_id in ids:
        message = gmail.fetch_message(service, message_id)
        if first is None:
            first = message
        pdfs = message.pdf_attachments()
        if pdfs:
            return message, pdfs[0], ""

    return first, None, (
        f"no PDF attachment in the {len(ids)} most recent matching messages — "
        "falling back to the newest message's BODY text"
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _head(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, 74 - len(title)))


def _last_audit_row(db_path: Path) -> dict | None:
    """Read back what was persisted. A direct read, not a new store API: this is a CLI
    confirming a write, not a second interface onto the audit table."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT id, ts, route, flag_reason, policy_hash, payload FROM audit "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    con.close()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.run_once",
        description="Fetch one real email, extract, route through the real gates, audit it.",
    )
    parser.add_argument("--query", default="has:attachment filename:pdf",
                        help="Gmail search query (default: %(default)r). "
                             "Pass '' to scan the whole inbox.")
    parser.add_argument("--scan", type=int, default=15,
                        help="How many recent messages to look through (default: %(default)s)")
    parser.add_argument("--folders", default=str(DEFAULT_FOLDERS_PATH),
                        help="known_folders JSON (default: %(default)s)")
    parser.add_argument("--policy", default=None, help="policy.yaml to load")
    parser.add_argument("--db", default=None, help="sqlite audit database path")
    parser.add_argument("--credentials", default=None, help="OAuth client secrets JSON")
    parser.add_argument("--token", default=None, help="cached OAuth token JSON")
    parser.add_argument("--no-browser", action="store_true",
                        help="print the consent URL instead of opening a browser")
    args = parser.parse_args(argv)

    _utf8_stdout()
    print(BANNER)

    # 1 — policy and database, through the composition root.
    # create_app() is the only place in this process that constructs a Policy (P2.12).
    # It returns a FastAPI object this CLI does not serve; borrowing it is cheaper than
    # opening a second construction path that could drift from the web surface's.
    _head("1. policy + store")
    app = create_app(policy_path=args.policy, db_path=args.db)
    policy = app.state.policy
    print(f"policy version {policy.version}, hash {policy.policy_hash}")
    print(f"known doc types: {', '.join(policy.known_doc_types)}")
    print(f"audit database: {store.DB}")

    # 2 — ground truth.
    _head("2. known folders (drive.py is still a stub)")
    try:
        known_folders = load_known_folders(Path(args.folders))
    except (FileNotFoundError, ValueError) as exc:
        print(f"REFUSED: {exc}")
        return 2
    for address, folder_id in known_folders.items():
        print(f"  {address!r} -> {folder_id}")

    # 3 — Gmail, read-only.
    _head("3. gmail auth (read-only)")
    try:
        service = gmail.build_client(
            credentials_path=args.credentials,
            token_path=args.token,
            open_browser=not args.no_browser,
        )
    except FileNotFoundError as exc:
        print(f"REFUSED: {exc}")
        return 2
    except RuntimeError as exc:
        print(f"REFUSED — scope assertion failed:\n{exc}")
        return 3
    print("client constructed; granted scopes asserted equal to TRIAGE_SCOPES")

    # 4 — pick a message.
    _head("4. selecting a message")
    print(f"query={args.query!r} scan={args.scan}")
    message, attachment, note = pick_message(service, args.scan, args.query or None)
    if message is None:
        print(f"nothing to do: {note}")
        return 1
    if note:
        print(f"NOTE: {note}")
    print(f"message  {message.id}  thread {message.thread_id}")
    print(f"from     {message.sender}")
    print(f"date     {message.date}")
    print(f"subject  {message.subject}")
    if attachment:
        print(f"pdf      {attachment.filename} ({attachment.size_bytes} bytes)")
    else:
        print(f"body     {len(message.body_text)} chars, source={message.body_source}")

    # 5 — extract.
    _head("5. extraction (claude-sonnet-4-6)")
    try:
        result = asyncio.run(_extract(service, message, attachment, policy))
    except (RuntimeError, ValueError) as exc:
        print(f"REFUSED: {exc}")
        return 2
    doc = result.doc
    for field in ("doc_type", "property_address", "client_name", "doc_date"):
        print(f"  {field:<18} {getattr(doc, field)!r}")
    print(f"  {'source_text':<18} {len(doc.source_text)} chars")
    if doc.parse_error:
        print(f"  {'parse_error':<18} {doc.parse_error!r}")
    print("\nthe model's own uncertainty notes:")
    for entry in result.uncertain:
        print(f"  - {entry}")
    if not result.uncertain:
        print("  (none)")
    print("\ngrounding pre-check (a prediction of Gate 1b, not a decision):")
    for field, verdict in result.span_check.items():
        print(f"  {field:<18} {verdict}")

    # 6 — the real gates. This call writes the audit row.
    _head("6. routing (validate.process_document — writes the audit row)")
    decision = process_document(doc, known_folders, policy, store)
    print(f"route        {decision.route.value.upper()}")
    if decision.flag_reason:
        print(f"flag_reason  {decision.flag_reason.value}")
    if decision.evidence.detail:
        print(f"detail       {decision.evidence.detail}")
    if decision.evidence.gates_passed:
        print(f"gates_passed {decision.evidence.gates_passed}")

    # 7 — what would have happened, and what was recorded.
    _head("7. action")
    if decision.route == Route.FILE:
        print(f"WOULD-FILE   {decision.canonical_name}")
        print(f"          ->  Drive folder {decision.target_folder_id}")
        print("NOT FILED. No Drive credential exists in this project and "
              "app/adapters/drive.py is a stub. Nothing was uploaded.")
    else:
        print("FLAGGED for human review. In a wired system the PDF would also be "
              "copied to the 'Needs review' folder; there is no Drive credential, so "
              "it was not.")

    _head("8. audit row (redacted — no source_text, by type)")
    row = _last_audit_row(Path(store.DB))
    if row is None:
        print("no audit row found — this should not happen; process_document always writes one")
        return 4
    print(f"id {row['id']}  ts {row['ts']}  route {row['route']}  "
          f"flag_reason {row['flag_reason']}  policy_hash {row['policy_hash']}")
    print(json.dumps(json.loads(row["payload"]), indent=2, ensure_ascii=False))

    # Belt and braces: the payload that was written and the payload redaction produces
    # for this decision should be the same object. If they are not, the row is not
    # about this run.
    expected = json.loads(redact(decision).model_dump_json())
    if json.loads(row["payload"]) != expected:
        print("\nWARNING: the last audit row does not match this decision.")
        return 4

    print("\nDone. Nothing was sent. Nothing was written outside the audit table.")
    return 0


async def _extract(service, message, attachment, policy):
    if attachment is not None:
        pdf_bytes = gmail.fetch_attachment_bytes(service, message.id, attachment)
        return await llm.extract_document_verbose(
            pdf_bytes, message.id, attachment.filename,
            known_doc_types=policy.known_doc_types,
        )

    # Body fallback. The "filename" recorded is the message id, because there is no
    # file — writing something that looks like one into the audit row would misdescribe
    # what was processed.
    text = f"Subject: {message.subject}\nFrom: {message.sender}\nDate: {message.date}\n\n{message.body_text}"
    return await llm.extract_text_verbose(
        text, message.id, f"(email body {message.id})",
        known_doc_types=policy.known_doc_types,
    )


if __name__ == "__main__":
    sys.exit(main())
