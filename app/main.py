"""main.py — FastAPI surface. Small on purpose.

No endpoint here puts mail on the wire and no endpoint accepts policy overrides.
Absence is not by itself enforcement, so neither property rests on this file:
the send law is enforced by app/scopes.py pinning the credential to
gmail.readonly, and policy immutability by Policy being frozen with a derived
hash (app/policy_loader.py).

Approval is not an endpoint. The review queue surfaces structured draft blocks;
the human composes and sends from their own Gmail.

Importing this module does nothing. create_app() is the composition root and the
only place in the process that constructs a Policy or touches the database — see
its docstring."""
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, field_validator
from app.policy_loader import load_policy
from app import store


class Correction(BaseModel):
    """A human's resolution of a flag, in the request BODY.

    It was `correction: str` as a bare scalar, which FastAPI reads as a query
    parameter — so the text of a mistake about a real client's real transaction went
    into every access log and proxy trace between here and the browser (finding L5.5).
    A body is not secrecy, but it is not a URL either.
    """

    correction: str

    @field_validator("correction")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("correction must not be empty")
        return v

def create_app(policy_path: str | Path | None = None,
               db_path: str | Path | None = None) -> FastAPI:
    """Build the app. This is the composition root: the one place that constructs a
    Policy and the one place that creates the database.

    It used to happen at module import — `policy = load_policy()` and `store.init()`
    as module-level statements — which meant importing this file read the filesystem
    and created a DB as a side effect, and a malformed policy.yaml raised at import.
    An import-time crash has no startup to fail at: it surfaces wherever the first
    import happens, including inside a test collector.

    Policy stays a per-call parameter at the functions that use it — that is what
    keeps the gate a pure function of (extraction, ground truth, policy), which is
    the property the audit praised. What is closed is construction: one policy is
    built here, per process, and a second one in flight is now a deliberate act
    rather than a default argument nobody noticed.
    """
    if db_path is not None:
        store.DB = Path(db_path)
    policy = load_policy(policy_path)
    store.init()

    app = FastAPI(title="Chief of Staff", version="0.1.0")
    app.state.policy = policy
    app.add_api_route("/health", health, methods=["GET"])
    app.add_api_route("/queue", queue, methods=["GET"])
    app.add_api_route("/queue/{audit_id}/resolve", resolve, methods=["POST"])
    app.add_api_route("/stats", stats, methods=["GET"])
    return app


def health(request: Request):
    policy = request.app.state.policy
    return {"ok": True, "policy_version": policy.version, "policy_hash": policy.policy_hash}

def queue():
    """The flag queue — a first-class surface, not a webhook."""
    return store.flag_queue()

def resolve(audit_id: int, body: Correction):
    """Append a correction to a flagged row. It does not overwrite one: resolving the
    same flag twice leaves two rows, and the queue derives 'resolved' from their
    existence rather than from a stored boolean."""
    try:
        correction_id = store.add_correction(audit_id, body.correction)
    except store.UnknownAuditRow:
        raise HTTPException(status_code=404, detail=f"no audit row with id {audit_id}")
    except store.NotAFlag:
        raise HTTPException(
            status_code=409,
            detail=f"audit row {audit_id} was not flagged; there is nothing to resolve",
        )
    return {"resolved": audit_id, "correction_id": correction_id}

def stats():
    """The case-study endpoint: total / auto-filed / flagged."""
    return store.stats()
