"""store.py — sqlite persistence: flag queue + append-only audit log.
The audit table doubles as the before/after case-study instrument.

The payload column holds a RedactedDecision, never a RoutingDecision: the document's
text layer is replaced by a hash and a short preview before it is written (see
app/redact.py, audit finding L5.1). That is enforced by the type record_decision
serializes, not by remembering to strip a field here.

APPEND-ONLY, stated exactly (finding A.4 — the old docstring made this claim on a
file whose next screenful was an UPDATE):

  - content redaction may destroy payload text in place;
  - decision fields (route, reason, hashes, timestamps) are never revised;
  - corrections append.

There is one UPDATE statement in this module, inside redact_legacy_rows, and
tests/test_corrections.py fails if a second one appears. A human's resolution of a
flag is an INSERT into `corrections`; "resolved" is derived by join and is not a
column, because a stored flag is a second source of truth that can disagree with the
corrections themselves."""
from __future__ import annotations
import sqlite3, json
from pathlib import Path
from app.models import RoutingDecision, Route
from app.redact import redact, redact_raw_payload

DB = Path(__file__).parent.parent / "data.sqlite"

class UnknownAuditRow(LookupError):
    """No audit row with that id."""

class NotAFlag(ValueError):
    """That row was not flagged, so there is nothing to resolve."""

def init():
    con = sqlite3.connect(DB)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS audit (
      id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP,
      route TEXT, flag_reason TEXT, policy_hash TEXT,
      payload TEXT
    );
    CREATE TABLE IF NOT EXISTS corrections (
      id INTEGER PRIMARY KEY,
      audit_id INTEGER NOT NULL,
      correction TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS corrections_by_audit ON corrections (audit_id);
    """)
    con.commit(); con.close()
    redact_legacy_rows()
    migrate_legacy_corrections()

def redact_legacy_rows() -> int:
    """Strip the text layer out of any row written before redaction existed.
    Returns the number of rows rewritten.

    Called from init() because CREATE TABLE IF NOT EXISTS means an upgrade keeps the
    existing table: redacting only new writes would leave untouched exactly the thing
    L5.1 is about, which is document text at rest. The rewrite is a deletion of
    content, not a revision of a decision — route, flag_reason, policy_hash and the
    decision fields are all left as they were recorded.
    """
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT id, payload FROM audit WHERE payload LIKE '%\"source_text\"%'"
    ).fetchall()
    rewritten = 0
    for rid, payload in rows:
        try:
            cleaned = redact_raw_payload(json.loads(payload))
        except (ValueError, TypeError):
            continue          # unparseable payload: leave it for a human, do not drop it
        if cleaned is None:
            continue
        con.execute("UPDATE audit SET payload=? WHERE id=?", (json.dumps(cleaned), rid))
        rewritten += 1
    con.commit(); con.close()
    return rewritten

def record_decision(d: RoutingDecision, policy_hash: str) -> int:
    con = sqlite3.connect(DB)
    cur = con.execute(
        "INSERT INTO audit (route, flag_reason, policy_hash, payload) VALUES (?,?,?,?)",
        (d.route.value, d.flag_reason.value if d.flag_reason else None,
         policy_hash, redact(d).model_dump_json()))
    con.commit(); rid = cur.lastrowid; con.close()
    return rid

def migrate_legacy_corrections() -> int:
    """Move corrections out of the old `audit.human_correction` column and into the
    corrections table. Returns the number moved; idempotent.

    Deriving resolution from a join is a change of shape, and a table written before
    that change holds its resolutions somewhere the join cannot see. Without this,
    upgrading would silently un-resolve every previously handled flag and put it back
    in front of a human — and the old docstring is right that corrections are the one
    thing here that cannot be regenerated.

    The old column is left in place rather than dropped: reading it stops here, and a
    destructive schema change to recover one unread column is not worth the failure
    modes. `init()` creates no such column for new databases.
    """
    con = sqlite3.connect(DB)
    if "human_correction" not in {r[1] for r in con.execute("PRAGMA table_info(audit)")}:
        con.close(); return 0
    rows = con.execute(
        "SELECT a.id, a.human_correction, a.ts FROM audit a "
        "WHERE a.human_correction IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM corrections c WHERE c.audit_id = a.id)").fetchall()
    for audit_id, correction, ts in rows:
        con.execute(
            "INSERT INTO corrections (audit_id, correction, created_at) VALUES (?,?,?)",
            (audit_id, correction, ts))
    con.commit(); con.close()
    return len(rows)

def flag_queue() -> list[dict]:
    """Open flags: route='flag' with no correction yet. Resolution is derived here,
    by the absence of a corrections row — never read from a column."""
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT a.id, a.ts, a.flag_reason, a.payload FROM audit a "
        "WHERE a.route=? AND NOT EXISTS "
        "(SELECT 1 FROM corrections c WHERE c.audit_id = a.id) "
        "ORDER BY a.ts, a.id", (Route.FLAG.value,)).fetchall()
    con.close()
    return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

def add_correction(audit_id: int, correction: str) -> int:
    """Human resolves a flag. Corrections are the free training data
    that shrinks the flag rate over time — never discard them.

    Appends; it does not overwrite. A second correction on the same row is a second
    row, because "what the human first thought" is data about the flag rate too.

    The id and route checks live here, not only in the HTTP endpoint. The endpoint is
    not the only possible caller — that is the L5.2 lesson applied to writes.
    """
    con = sqlite3.connect(DB)
    row = con.execute("SELECT route FROM audit WHERE id=?", (audit_id,)).fetchone()
    if row is None:
        con.close()
        raise UnknownAuditRow(f"no audit row with id {audit_id}")
    if row[0] != Route.FLAG.value:
        con.close()
        raise NotAFlag(f"audit row {audit_id} routed '{row[0]}', not a flag")
    cur = con.execute(
        "INSERT INTO corrections (audit_id, correction) VALUES (?,?)",
        (audit_id, correction))
    con.commit(); cid = cur.lastrowid; con.close()
    return cid

def corrections_for(audit_id: int) -> list[dict]:
    """Every correction on a row, oldest first. Ordered by created_at then id:
    CURRENT_TIMESTAMP is second-granular, so id is what breaks the tie between two
    corrections entered in the same second. Latest wins; earlier ones are kept."""
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, audit_id, correction, created_at FROM corrections "
        "WHERE audit_id=? ORDER BY created_at, id", (audit_id,)).fetchall()
    con.close()
    return [dict(r) for r in rows]

def latest_correction(audit_id: int) -> dict | None:
    rows = corrections_for(audit_id)
    return rows[-1] if rows else None

def stats() -> dict:
    con = sqlite3.connect(DB)
    total = con.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    filed = con.execute("SELECT COUNT(*) FROM audit WHERE route='file'").fetchone()[0]
    flagged = con.execute("SELECT COUNT(*) FROM audit WHERE route='flag'").fetchone()[0]
    con.close()
    return {"total": total, "auto_filed": filed, "flagged": flagged, "misfiled": "measure manually — target 0"}
