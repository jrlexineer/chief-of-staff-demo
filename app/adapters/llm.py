"""llm.py — extraction + drafting adapter.

EXTRACTION IS WIRED (LIVE.2). DRAFTING IS STILL A STUB, deferred to N5 with the rest
of L2.

Contract, unchanged and now enforced by the prompt below: the extractor MUST emit
null for any field it cannot read verbatim from the document. Nulls are cheap (they
flag); guesses are expensive (they misfile).

WHAT THIS ADAPTER DOES NOT DO
-----------------------------
It does not decide anything. `validate._route_document` is the gate, and it checks
every routing field for containment in `source_text` itself. This module reports what
it found — including, in `Extraction.span_check`, whether each value it is handing
over is actually present in the source — and then passes the model's answer through
UNCHANGED.

That last part is deliberate and it is the easiest thing here to get wrong. When the
model returns a value that is not in the document, the tempting fix is to null it out
in the adapter. Doing so would convert a hallucination into a missing field: the
document still flags, but as NULL_FIELD rather than UNGROUNDED_FIELD, and the one
signal that says "the model invented something" is gone from the audit row. Report,
do not repair. The law stays in validate.py.

THE PROMPT
----------
`EXTRACTION_PROMPT` carries the three fixes the extraction spike concluded with
(PROJECT_STATUS.md, branch `spike/extraction`, 2026-08-29). The spike's one real
failure was not a hallucination — it was a CONSTRUCTED value in a content field,
`"her (unnamed woman who called twice)"`, a string that appears nowhere in the source
and therefore fails substring grounding outright. Three rules address it:

  1. A content field takes a QUOTABLE SPAN of the source. If naming the thing
     requires constructing a label, it is not a value — it is an `uncertain` entry.
     This is the one that matters for L2, and it is the same argument D5 makes for
     `doc_type_markers`: a field whose value cannot be found in the source cannot be
     verified by any mechanism, only by reading.
  2. The "one place" rule is stated as a check on the OUTPUT, not as an instruction
     about intent. The spike broke it while following its prose.
  3. Each field's scope is defined, because `amounts` was being read as "any
     quantity" purely because nothing said otherwise.

`doc_type` is the single exception to rule 1 and the prompt says so outright: a
classification is not a quotation (D5). Nothing checks it as evidence yet — the
marker gate is N6 — so the prompt asks for conservatism instead, and an unrecognised
type comes back null and flags.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
from dataclasses import dataclass, field

from app.models import ExtractedDoc
from app.redact import PREVIEW_CHARS

# The gate's own normalizer, not a second one that agrees today. `span_check` below is
# a prediction of what validate.py will conclude; if the two normalize differently the
# report is worse than no report. This is finding A.6's drift, avoided by import.
from app.pipeline.validate import ROUTING_FIELDS, _normalize

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1000

#: Room for the "extraction failed: " label and the exception's class name, on top of
#: the PREVIEW_CHARS budget the detail itself gets. Same rule as validate.py's
#: EXTRACTION_ERROR path (P2.9): an error string that quotes what it choked on would
#: put document text back into the audit row through the one field redaction does not
#: reach.
ERROR_PREFIX_BUDGET = 80

#: Below this many non-whitespace characters a PDF has no usable text layer — it is a
#: scan. Sending it anyway would ask the model to read an image it was never given.
MIN_TEXT_CHARS = 40

#: The fields the model is asked for. doc_type is listed first because it is the one
#: that is a label rather than a span.
CONTENT_FIELDS = ("doc_type", "property_address", "client_name", "doc_date")

DEFAULT_DOC_TYPES = ("contract", "addendum", "disclosure", "amendment")


EXTRACTION_PROMPT = """\
You extract filing fields from a real-estate document. You are a transcriber, not an
analyst. The text below is the document's own text layer, exactly as it was read.

Return STRICT JSON only. No prose before or after it, no markdown fences, no
explanation. Exactly these keys, all of them present:

{
  "doc_type": null,
  "property_address": null,
  "client_name": null,
  "doc_date": null,
  "uncertain": []
}

FIELD SCOPES. Each field has one meaning; do not widen it.

- "doc_type" — what kind of document this is, as ONE of: {doc_types}. If it is not
  clearly one of those, return null. Do not stretch a near-match to fit.
- "property_address" — the street address of the property THIS document is about, as
  written. Not a county, not a parcel number, not a mailing address for
  correspondence, and not a second property mentioned in passing.
- "client_name" — the named party this document is executed by or on behalf of, as
  written. A person or an entity. Not an agent, not a broker, not a title company,
  unless that party is itself the named signatory.
- "doc_date" — the date the document itself carries, as written ("March 3, 2026",
  "3/3/26"). Do not normalize it to a format it does not use. Not today's date, not a
  deadline mentioned in the body, not a signature date unless that is the only date
  the document carries — and if you are choosing between two dates, that choice is an
  "uncertain" entry, not a value.

RULE 1 — A VALUE IS A QUOTABLE SPAN. Every field except "doc_type" must be a span of
text you can point at in the document above. Copy it. If naming the thing would
require you to construct a label, complete a partial address, resolve a pronoun, join
two fragments, or write a parenthetical explaining who or what is meant, then it is
NOT a value. It is null, and what is missing goes in "uncertain".

  Wrong: "client_name": "the buyer's sister (name not stated)"
  Right: "client_name": null,
         "uncertain": ["signatory referred to as 'the buyer's sister'; no name given"]

"doc_type" is the ONE exception, because a classification is not a quotation — a
contract rarely contains the word "contract" in any form that means anything. Choose
it from the closed list above on the evidence of what the document is. Because it is
the one field that cannot be checked against the text as itself, be conservative:
null is the correct answer when the document does not clearly present as one of them.

RULE 2 — ONE PLACE. Before you answer, re-read your own JSON. If a value appears in a
field AND something about that same value appears in "uncertain", you have put it in
two places. Decide which: a value you copied from the document stays in the field; a
value you worked out stays in "uncertain" and the field is null.

RULE 3 — NULL IS AN ANSWER. A document that does not state a client name has
"client_name": null. Do not fill a field from context, from the filename, from a
similar document, or from what would make the record complete. Never infer, never
guess. Null is the correct answer when uncertain, and naming what is missing in
"uncertain" is useful output, not a failure.

Document text follows.

---
{document_text}
---

JSON only."""


@dataclass
class Extraction:
    """What the extractor found, plus what is worth knowing about how it found it.

    `doc` is the only thing the pipeline consumes. The rest exists so a human reading
    a run can see the model's own uncertainty notes and the grounding check, neither
    of which fits in `ExtractedDoc` — and adding them to `ExtractedDoc` would change
    the audit row's shape (app/redact.py has field parity with it), which is a bigger
    decision than this session is making.
    """

    doc: ExtractedDoc
    uncertain: list[str] = field(default_factory=list)
    span_check: dict[str, str] = field(default_factory=dict)   # field -> grounded|ungrounded|null
    raw: dict | None = None
    model: str | None = None

    def ungrounded_fields(self) -> list[str]:
        return [f for f, verdict in self.span_check.items() if verdict == "ungrounded"]


def build_client():
    """An AsyncAnthropic client, or a RuntimeError that says how to fix it."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set, so there is nothing to call.\n"
            '  PowerShell, this terminal:  $env:ANTHROPIC_API_KEY = "sk-ant-..."\n'
            '  PowerShell, persisted:      setx ANTHROPIC_API_KEY "sk-ant-..."\n'
            '  bash:                       export ANTHROPIC_API_KEY="sk-ant-..."\n'
            "A key comes from https://console.anthropic.com/settings/keys."
        )
    import anthropic

    return anthropic.AsyncAnthropic()


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

async def extract_document(
    pdf_bytes: bytes, message_id: str, filename: str, *,
    known_doc_types=None, client=None,
) -> ExtractedDoc:
    """The pipeline's entry point, unchanged: PDF in, ExtractedDoc out.

    filing.handle_inbound_pdf awaits this and expects exactly this shape. The richer
    return lives in extract_document_verbose so wiring a fuller surface did not change
    the contract the pipeline was already written against.
    """
    result = await extract_document_verbose(
        pdf_bytes, message_id, filename, known_doc_types=known_doc_types, client=client
    )
    return result.doc


async def extract_document_verbose(
    pdf_bytes: bytes, message_id: str, filename: str, *,
    known_doc_types=None, client=None,
) -> Extraction:
    """Text layer first, then the model call over that text.

    The text layer is produced by pypdf, not by the model. That is what makes the
    grounding gate mean anything: if the model both transcribed the document and
    extracted from it, containment would only prove it was consistent with itself.
    """
    text, error = pdf_text(pdf_bytes)
    if error is not None:
        return _failed(message_id, filename, text or "", error)

    return await extract_text_verbose(
        text, message_id, filename, known_doc_types=known_doc_types, client=client
    )


async def extract_text(
    source_text: str, message_id: str, filename: str, *,
    known_doc_types=None, client=None,
) -> ExtractedDoc:
    """Same as extract_text_verbose, discarding the extras."""
    result = await extract_text_verbose(
        source_text, message_id, filename, known_doc_types=known_doc_types, client=client
    )
    return result.doc


async def extract_text_verbose(
    source_text: str, message_id: str, filename: str, *,
    known_doc_types=None, client=None,
) -> Extraction:
    """Extract from a text layer that is already in hand — a PDF's, or an email body's.

    Every failure below returns an Extraction carrying `parse_error` rather than
    raising. An exception here escapes upstream of `store.record_decision` and the
    document is lost with no flag, no audit row and no queue copy — finding L5.2's
    shape, on the extraction side of the gate.
    """
    if client is None:
        client = build_client()

    types = list(known_doc_types or DEFAULT_DOC_TYPES)
    prompt = (
        EXTRACTION_PROMPT
        .replace("{doc_types}", ", ".join(types))
        .replace("{document_text}", source_text)
    )

    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:                       # noqa: BLE001 — deliberate catch-all
        return _failed(
            message_id, filename, source_text,
            f"extraction failed: {type(exc).__name__}: {exc}",
        )

    if getattr(response, "stop_reason", None) == "max_tokens":
        return _failed(
            message_id, filename, source_text,
            f"extraction failed: response hit max_tokens ({MAX_TOKENS}); the JSON is "
            "truncated and a partial extraction is not a partial answer",
        )

    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")

    try:
        parsed = _parse_json(text)
    except ValueError as exc:
        return _failed(
            message_id, filename, source_text, f"extraction failed: {exc}"
        )

    doc = ExtractedDoc(
        source_message_id=message_id,
        original_filename=filename,
        source_text=source_text,
        **{f: _clean(parsed.get(f)) for f in CONTENT_FIELDS},
    )
    return Extraction(
        doc=doc,
        uncertain=_string_list(parsed.get("uncertain")),
        span_check=span_check(doc),
        raw=parsed,
        model=MODEL,
    )


def pdf_text(pdf_bytes: bytes) -> tuple[str, str | None]:
    """(text, error). A PDF with no usable text layer is an error, not empty text.

    A scan produces a garbage — or absent — text layer, and validate.py's docstring
    already relies on that: nothing the model claims to have read would be found in
    it, so the document flags naturally. Catching it here means the flag says "no text
    layer" instead of listing four ungrounded fields.
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:                       # noqa: BLE001 — any pypdf failure
        return "", f"could not read PDF: {type(exc).__name__}: {exc}"

    if len(text.strip()) < MIN_TEXT_CHARS:
        return text, (
            f"PDF has no usable text layer ({len(text.strip())} characters, minimum "
            f"{MIN_TEXT_CHARS}) — most likely a scan. Not sent to the extractor."
        )
    return text, None


def span_check(doc: ExtractedDoc) -> dict[str, str]:
    """Per routing field: grounded | ungrounded | null.

    A prediction of what validate.py's Gate 1b will conclude, computed with the gate's
    own normalizer. It is a report, not an intervention — nothing here changes `doc`.
    """
    haystack = _normalize(doc.source_text)
    out = {}
    for f in ROUTING_FIELDS:
        value = getattr(doc, f, None)
        if value is None or not str(value).strip():
            out[f] = "null"
        elif _normalize(str(value)) in haystack:
            out[f] = "grounded"
        else:
            out[f] = "ungrounded"
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _failed(message_id: str, filename: str, source_text: str, detail: str) -> Extraction:
    doc = ExtractedDoc(
        source_message_id=message_id,
        original_filename=filename,
        source_text=source_text,
        parse_error=detail[: PREVIEW_CHARS + ERROR_PREFIX_BUDGET],
    )
    return Extraction(doc=doc, span_check=span_check(doc), model=MODEL)


def _parse_json(text: str) -> dict:
    """Pull a JSON object out of a model response.

    The prompt forbids fences and prose, but an adapter that dies on a stray ``` fails
    a real document for a formatting slip, and a flag a human cannot act on is worse
    than a lenient parser.
    """
    body = (text or "").strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[: -len("```")]
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in the model's response")
    try:
        parsed = json.loads(body[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"response was not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("response JSON was not an object")
    return parsed


def _clean(value):
    """Normalize the model's answer to `str | None`. Empty and whitespace are null."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    return value or None


def _string_list(value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    return [str(v) for v in value]


# ---------------------------------------------------------------------------
# Drafting — STILL A STUB. Deferred to N5 with the rest of L2.
# ---------------------------------------------------------------------------

DRAFTING_PROMPT = """You draft replies to real-estate email threads on behalf of
a human, who will read and edit every draft before it goes anywhere.

A draft may contain ONLY these three kinds of content:

  (a) Facts stated verbatim in the thread you were given. If it is not in the
      thread, you do not know it. You may quote or restate a fact from the
      thread; you may not extend it.
  (b) Process and scheduling language — acknowledgement, next steps, who does
      what, when a call could happen, what you are waiting on.
  (c) Bracketed placeholders, in exactly this form:
          [CONFIRM: <what the human must supply>]
      wherever substantive deal terms, numbers, dates of commitment, or
      concessions would go.

The draft must NEVER invent, infer, or estimate deal substance. Do not compute
a number from other numbers. Do not carry a figure over from a similar deal. Do
not soften a missing term into vague language to avoid the placeholder — vague
substance is still invented substance.

Placeholders are MANDATORY, not optional. If substance is required to make the
reply coherent and that substance is not stated verbatim in the thread, you must
emit a [CONFIRM: ...] placeholder. Naming what is missing is the correct output.

Examples of correct placeholders:
  "We can close on [CONFIRM: closing date the seller will commit to]."
  "Our response is [CONFIRM: counter-offer price] with [CONFIRM: which
   contingencies we are keeping]."
  "I can cover [CONFIRM: dollar amount of the repair credit, if any]."

Tone: warm, brief, human. No links, no upsell."""


async def draft_reply(thread_context: str, category: str) -> str:
    """Draft a reply using DRAFTING_PROMPT. Returns the draft body.

    STILL A STUB — and deliberately so. D3 supersedes this shape: a draft is not
    prose, it is structured blocks (grounded facts, a reply skeleton, [CONFIRM:] as a
    FIELD rather than a substring, a send-checklist). Wiring this function to the API
    as free text would build the thing D3 decided against, and L2.1's missing
    post-generation gate has nothing to check a string against. N5 designs the schema;
    the drafting call comes after it.

    A draft containing an unfilled [CONFIRM:] is expected output, not an error. Do not
    retry, do not post-process it away, do not filter such drafts out — the human
    fills the bracket in Gmail.
    """
    raise NotImplementedError(
        "drafting is deferred to N5 (D3 structured blocks). Extraction is wired; "
        "see extract_document. PROJECT_STATUS.md carries the reasoning."
    )
