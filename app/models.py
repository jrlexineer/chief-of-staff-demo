"""models.py — shared schemas. Every pipeline speaks these types."""

from __future__ import annotations
from enum import Enum
from pydantic import BaseModel, Field


class FlagReason(str, Enum):
    PARSE_ERROR = "parse_error"
    EXTRACTION_ERROR = "extraction_error"   # the gate itself raised; see
                                            # validate.process_document. A crash is
                                            # a flag, never a silently dropped doc.
    UNGROUNDED_FIELD = "ungrounded_field"
    UNKNOWN_DOC_TYPE = "unknown_doc_type"
    NULL_FIELD = "null_field"
    NO_MATCHING_FOLDER = "no_matching_folder"
    AMBIGUOUS_MATCH = "ambiguous_match"


class Route(str, Enum):
    FILE = "file"          # all gates passed → automated action
    FLAG = "flag"          # any gate failed → human queue, with reason


class ExtractedDoc(BaseModel):
    """What the LLM extractor returns for one inbound document, plus the
    text it was reading. The extractor is REQUIRED to emit nulls rather
    than guesses — that requirement lives in the extraction prompt
    (adapters/llm.py) — and validate.py checks the claim against
    source_text rather than trusting it.
    """
    source_message_id: str
    original_filename: str
    source_text: str                       # the document's extracted text layer,
                                           # provided by the caller. Required: every
                                           # routing field must be findable inside it.
                                           # Never persisted: app/redact.py replaces it
                                           # with a hash + short preview before an audit
                                           # row is written (finding L5.1).
    doc_type: str | None = None            # e.g. "addendum", "contract", "disclosure"
    property_address: str | None = None
    client_name: str | None = None
    doc_date: str | None = None
    parse_error: str | None = None


class Evidence(BaseModel):
    gates_passed: list[str] = Field(default_factory=list)
    detail: str | None = None


class RoutingDecision(BaseModel):
    route: Route
    doc: ExtractedDoc
    target_folder_id: str | None = None
    canonical_name: str | None = None
    flag_reason: FlagReason | None = None
    evidence: Evidence


# AuditRecord used to sit here: a model of an audit row that nothing constructed,
# while store.py hand-rolled the SQL (finding B.2). Redaction made it actively wrong —
# it declared `decision: RoutingDecision`, the shape the audit table now refuses to
# hold. The written shape is app.redact.RedactedDecision plus the columns in
# store.init(); a second, unused description of the same row is how those two drift.


class TriageDraft(BaseModel):
    """A drafted reply, surfaced in this service's own review queue for the human
    to copy from; they compose and send in their own Gmail.

    The no-send property is enforced by app/scopes.py, which pins this service's
    credential to gmail.readonly and refuses to construct a client holding
    anything else. Do not treat `grep -r 'send' app/` as the check — it searches
    for a token, not a capability, and a wrapper named dispatch/deliver/transmit
    defeats it entirely.
    """
    thread_id: str
    category: str                          # "active_deal" | "waiting_on_them" | "new_contact"
    summary: str
    draft_body: str
    deal_reference: str | None = None      # resolved deal, or None → shown w/o auto-attach
