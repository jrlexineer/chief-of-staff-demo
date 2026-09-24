"""redact.py — the only shape a decision may take on its way into the audit table.

Audit finding L5.1: `store.record_decision` serialized the whole `RoutingDecision`
with `model_dump_json()`, which embeds `ExtractedDoc.source_text` — the complete text
layer of a legal document, PII and deal terms included — indefinitely, in a table
whose whole purpose is to be kept.

The fix is a type, not a habit. `RedactedDoc` has no `source_text` field at all, so
there is no version of this code that persists the text layer by forgetting to strip
it: constructing the object the writer accepts *is* the stripping. What survives is
what an audit row needs and no more:

  - `source_text_sha256` — proves a stored decision was made about THIS document, and
    that the document has not changed since. That is the audit's actual job; it never
    needed the contents to do it.
  - `source_text_preview` — the first PREVIEW_CHARS characters, so a human working the
    flag queue can recognise which scan they are looking at.
  - `source_text_chars` — the true length, so a truncated preview is visibly truncated
    rather than looking like a short document.

The preview is a deliberate, bounded disclosure, not a redaction gap: the head of a
document is usually letterhead and a title, but it can carry a name or an address.
It is sized to the review queue's need to identify a document. Widening it is a
decision about how much of a contract the archive holds, so it belongs here as a
named constant rather than inline at a call site.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict

from app.models import Evidence, ExtractedDoc, FlagReason, Route, RoutingDecision

PREVIEW_CHARS = 200


class RedactedDoc(BaseModel):
    """`ExtractedDoc` minus its text layer, plus the digest that replaces it.

    Field parity with `ExtractedDoc` is pinned by
    `tests/test_redaction.py::test_every_extracted_doc_field_is_accounted_for`, so a
    new field on the extraction is either carried into the audit row deliberately or
    dropped from it deliberately — never by nobody noticing.
    """

    model_config = ConfigDict(frozen=True)

    source_message_id: str
    original_filename: str
    source_text_sha256: str
    source_text_preview: str
    source_text_chars: int
    doc_type: str | None = None
    property_address: str | None = None
    client_name: str | None = None
    doc_date: str | None = None
    parse_error: str | None = None


class RedactedDecision(BaseModel):
    """What `store.record_decision` writes. Same decision, no document."""

    model_config = ConfigDict(frozen=True)

    route: Route
    doc: RedactedDoc
    target_folder_id: str | None = None
    canonical_name: str | None = None
    flag_reason: FlagReason | None = None
    evidence: Evidence


def _digest(text: str) -> dict:
    return {
        "source_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "source_text_preview": text[:PREVIEW_CHARS],
        "source_text_chars": len(text),
    }


def redact_doc(doc: ExtractedDoc) -> RedactedDoc:
    return RedactedDoc(**doc.model_dump(exclude={"source_text"}), **_digest(doc.source_text))


def redact(decision: RoutingDecision) -> RedactedDecision:
    return RedactedDecision(
        **decision.model_dump(exclude={"doc"}), doc=redact_doc(decision.doc)
    )


def redact_raw_payload(payload: dict) -> dict | None:
    """Redact an already-serialized audit payload in place, or return None if it is
    already clean.

    For rows written before this module existed. It works on the raw dict rather than
    reconstructing an `ExtractedDoc` on purpose: an old row must be redactable even if
    its shape no longer validates against today's models. Fixing exposure at rest
    cannot be contingent on the exposed data being well-formed.
    """
    doc = payload.get("doc")
    if not isinstance(doc, dict) or "source_text" not in doc:
        return None
    text = doc.pop("source_text")
    doc.update(_digest(text if isinstance(text, str) else str(text)))
    return payload
