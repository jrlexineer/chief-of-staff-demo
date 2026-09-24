"""
validate.py — the deterministic gate. THE core of this product.

Design law: the LLM reads; this module decides whether the read is
trustworthy. No model self-confidence is ever consulted. Routing is
based only on checks that are verifiable in code:

  - every routing field is CONTAINED in the document's own text layer
  - required fields present and non-null
  - extracted address resolves to EXACTLY ONE transaction folder
  - document type is one we know how to file

The containment gate replaces the old OCR-floor and handwriting gates,
which asked for numbers nobody can actually compute. Containment is
computable with a substring test, and it subsumes what those gates were
reaching for: a garbage scan produces a garbage text layer, so nothing
the model claims to have read will be found in it, and the document
flags naturally. No separate detector needed.

Anything that fails ANY gate → Flagged(reason, evidence). Never guess.
A misfiled contract is a silent failure; a flagged one is a 30-second
human task. Fail closed, always.

Public entry point: process_document(). The gate itself, _route_document(), is
private so that no caller can obtain a decision without an audit row being
written — a crash inside it becomes a flag, not a lost document.
"""

from __future__ import annotations
from app.models import (
    ExtractedDoc, RoutingDecision, Route, FlagReason, Evidence,
)
from app.policy_loader import Policy
from app.redact import PREVIEW_CHARS

# The fields a filing decision is actually made on. These — and only these —
# must be grounded in the document's text layer.
ROUTING_FIELDS = ("property_address", "client_name")


def process_document(
    doc: ExtractedDoc,
    known_folders: dict[str, str],   # normalized address -> drive folder id
    policy: Policy,
    store,
) -> RoutingDecision:
    """The only public way to obtain a routing decision. Always writes an audit row.

    The gate below is pure and private. Making it private is the point: a decision
    that reaches a caller without a corresponding audit row is the failure this
    wrapper exists to prevent, and previously nothing structural forced the write —
    `store.record_decision` was a voluntary call made in exactly one place, so any
    second consumer (batch re-file, dark-launch script, CLI) routed silently.

    A crash inside the gate becomes a FLAG the human can see rather than an
    exception that loses the document upstream of the audit write.

    If the audit write itself fails, that exception propagates and the caller never
    receives a decision to act on. Filing a document we cannot account for is worse
    than not filing it.
    """
    try:
        decision = _route_document(doc, known_folders, policy)
    except Exception as exc:                      # noqa: BLE001 — deliberate catch-all
        # The message is truncated, not trusted. A parser that quotes the text it
        # choked on would otherwise put the document back into the audit row through
        # the one field redaction does not reach. Same budget as the payload preview,
        # named in the same module, so "how much document text may a row hold" has
        # one answer.
        decision = _flag(
            doc, FlagReason.EXTRACTION_ERROR,
            f"Routing raised {type(exc).__name__}: {str(exc)[:PREVIEW_CHARS]}",
        )

    store.record_decision(decision, policy.policy_hash)
    return decision


def _route_document(
    doc: ExtractedDoc,
    known_folders: dict[str, str],   # normalized address -> drive folder id
    policy: Policy,
) -> RoutingDecision:
    """Pure function: (extraction, ground truth, policy) -> decision.
    No I/O, no model calls. Trivially unit-testable, which is the point.

    Private on purpose — call process_document(), which audits. Tests may call this
    directly; production code may not.
    """

    # Gate 1 — extraction integrity -------------------------------------
    if doc.parse_error:
        return _flag(doc, FlagReason.PARSE_ERROR, doc.parse_error)

    # Gate 1b — grounding: every routing field must be IN the document ---
    # Not "does the model feel sure" but "is the string it returned actually
    # present in the text it was given". A null here is not a grounding
    # failure — absence is Gate 2's business.
    #
    # Unconditional by construction. This gate used to sit behind
    # `if policy.grounding_required:`, which made the load-bearing check a
    # boolean in a config file. There is no longer a policy value that reaches
    # this code path.
    haystack = _normalize(doc.source_text)
    for field in ROUTING_FIELDS:
        value = getattr(doc, field, None)
        if value is None or not value.strip():
            continue
        if _normalize(value) not in haystack:
            return _flag(
                doc, FlagReason.UNGROUNDED_FIELD,
                f"Field '{field}' = '{value}' does not appear in the "
                f"document's own text layer",
            )

    # Gate 2 — required fields, per doc type ----------------------------
    if doc.doc_type not in policy.known_doc_types:
        return _flag(
            doc, FlagReason.UNKNOWN_DOC_TYPE,
            f"Extracted type '{doc.doc_type}' is not in policy.known_doc_types",
        )

    # .get(), not [] — an unguarded index here raises upstream of
    # store.record_decision, which is fail-open into silence: no flag, no audit
    # row, no review-queue copy. load_policy() forbids the skew that reaches this
    # branch, but a hand-built Policy can still carry it, so the gate flags.
    required = policy.required_fields.get(doc.doc_type)
    if required is None:
        return _flag(
            doc, FlagReason.UNKNOWN_DOC_TYPE,
            f"Type '{doc.doc_type}' has no entry in policy.required_fields",
        )

    for field in required:
        value = getattr(doc, field, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            return _flag(
                doc, FlagReason.NULL_FIELD,
                f"Required field '{field}' is null/empty for type '{doc.doc_type}'",
            )

    # Gate 3 — ground-truth resolution: the folder tree IS the registry -
    matches = _resolve_folder(doc.property_address, known_folders)

    if len(matches) == 0:
        return _flag(
            doc, FlagReason.NO_MATCHING_FOLDER,
            f"'{doc.property_address}' matches no active transaction folder",
        )
    if len(matches) > 1:
        return _flag(
            doc, FlagReason.AMBIGUOUS_MATCH,
            f"'{doc.property_address}' matches {len(matches)} folders: {matches}",
        )

    # All gates passed → file it.
    folder_key = matches[0]
    return RoutingDecision(
        route=Route.FILE,
        target_folder_id=known_folders[folder_key],
        canonical_name=_canonical_name(doc),
        evidence=Evidence(gates_passed=["parse", "grounding", "fields", "resolution"]),
        doc=doc,
    )


def _resolve_folder(address: str | None, known: dict[str, str]) -> list[str]:
    """Exact-match on normalized address. Deliberately strict for v1.
    Loosening this (fuzzy match) requires a policy change + eval run,
    never an inline tweak. Strictness costs flags; guessing costs trust.
    """
    if not address:
        return []
    needle = _normalize(address)
    return [k for k in known if _normalize(k) == needle]


def _normalize(s: str) -> str:
    return " ".join(s.lower().replace(".", "").replace(",", "").split())


def _canonical_name(doc: ExtractedDoc) -> str:
    addr = "".join(c for c in (doc.property_address or "") if c.isalnum())
    client = "".join(c for c in (doc.client_name or "") if c.isalnum())
    return f"{doc.doc_type}_{addr}_{client}.pdf"


def _flag(doc: ExtractedDoc, reason: FlagReason, detail: str) -> RoutingDecision:
    return RoutingDecision(
        route=Route.FLAG,
        flag_reason=reason,
        evidence=Evidence(detail=detail),
        doc=doc,
    )
