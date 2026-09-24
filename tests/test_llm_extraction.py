"""test_llm_extraction.py — the extraction adapter, with the API mocked.

Covers finding L2.3 (zero test coverage on llm.py) for the extraction half. The
drafting half is still untested and still deferred to N5.

Three groups:

  1. **The prompt carries the spike's fixes.** The spike (PROJECT_STATUS.md, branch
     `spike/extraction`) found one real failure: a *constructed* label in a content
     field — `"her (unnamed woman who called twice)"` — which appears nowhere in the
     source and so fails substring grounding outright. The fix was three prompt
     rules. These tests pin them, because L2.2's shape applies here: a prompt string
     nothing asserts against can be edited away with no test failing.

  2. **The adapter reports, it does not repair.** When the model returns a value that
     is not in `source_text`, the adapter records that it is ungrounded and passes it
     through UNCHANGED, so `validate.py` flags UNGROUNDED_FIELD. Nulling it in the
     adapter would launder a hallucination into a missing field and hide it from the
     one gate whose job it is.

  3. **Failure is a flag, not an exception.** No text layer, an API outage, an
     unparseable response — each returns an `ExtractedDoc` carrying `parse_error`, so
     the document reaches the audit table and the queue instead of vanishing upstream
     of the write (the shape of finding L5.2).

    python -m pytest tests/test_llm_extraction.py
"""
import asyncio
import io
import json

import pytest

from app.adapters import llm
from app.models import ExtractedDoc
from app.redact import PREVIEW_CHARS

SOURCE = (
    "ADDENDUM TO PURCHASE AGREEMENT\n"
    "Property: 12 Oak St, Springfield\n"
    "Buyer: Dana Whitfield\n"
    "Dated: March 3, 2026\n"
)


class FakeMessages:
    def __init__(self, text=None, error=None):
        self._text, self._error = text, error
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return FakeResponse(self._text)


class FakeResponse:
    def __init__(self, text):
        self.stop_reason = "end_turn"
        self.content = [FakeBlock(text)]


class FakeBlock:
    def __init__(self, text):
        self.type, self.text = "text", text


class FakeClient:
    def __init__(self, text=None, error=None):
        self.messages = FakeMessages(text, error)


def payload(**overrides):
    body = {
        "doc_type": "addendum",
        "property_address": "12 Oak St, Springfield",
        "client_name": "Dana Whitfield",
        "doc_date": "March 3, 2026",
        "uncertain": [],
    }
    body.update(overrides)
    return json.dumps(body)


def run(coro):
    return asyncio.run(coro)


def extract(text=None, *, source=SOURCE, error=None, **kwargs):
    client = FakeClient(text, error)
    result = run(llm.extract_text_verbose(source, "msg-1", "addendum.pdf", client=client, **kwargs))
    return result, client


# --------------------------------------------------------------------------
# 1. The prompt carries the spike's fixes
# --------------------------------------------------------------------------

def test_prompt_requires_a_quotable_span_for_every_content_field():
    """Spike fix 1, the one that matters for L2: a field whose value cannot be found
    in the source cannot be verified by any mechanism, only by reading."""
    prompt = llm.EXTRACTION_PROMPT.lower()
    assert "quotable span" in prompt
    assert "construct" in prompt          # ...a label -> it is not a value
    assert "uncertain" in prompt


def test_prompt_states_the_one_place_rule_as_a_check_on_the_output():
    """Spike fix 2. Trap 2 broke the 'one place' rule while following its prose, so
    the rule is restated as something the model re-reads its own JSON against."""
    prompt = llm.EXTRACTION_PROMPT.lower()
    assert "before you answer" in prompt or "re-read" in prompt
    assert "one place" in prompt


def test_prompt_defines_the_scope_of_each_field():
    """Spike fix 3. `amounts` was read as 'any quantity' because the prompt never said
    otherwise; these four fields get an explicit scope each."""
    for field in ("doc_type", "property_address", "client_name", "doc_date"):
        assert field in llm.EXTRACTION_PROMPT


def test_prompt_names_doc_type_as_the_one_classification():
    """D5: a classification is not a quotation. doc_type is the single field that is a
    label rather than a span, and the prompt has to say so or rule 1 contradicts it."""
    assert "doc_type" in llm.EXTRACTION_PROMPT
    assert "classification" in llm.EXTRACTION_PROMPT.lower()


def test_the_closed_doc_type_list_is_passed_to_the_model():
    _, client = extract(payload(), known_doc_types=["contract", "addendum"])
    sent = client.messages.calls[0]["messages"][0]["content"]
    assert "contract" in sent and "addendum" in sent


def test_uses_the_model_the_spike_was_graded_on():
    """The spike's verdict is evidence about claude-sonnet-4-6 with this prompt shape.
    Changing the model silently makes that verdict evidence about something else."""
    _, client = extract(payload())
    assert client.messages.calls[0]["model"] == llm.MODEL == "claude-sonnet-4-6"
    assert client.messages.calls[0]["max_tokens"] == llm.MAX_TOKENS


# --------------------------------------------------------------------------
# 2. The adapter reports, it does not repair
# --------------------------------------------------------------------------

def test_a_clean_extraction_becomes_an_extracted_doc():
    result, _ = extract(payload())
    assert isinstance(result.doc, ExtractedDoc)
    assert result.doc.doc_type == "addendum"
    assert result.doc.property_address == "12 Oak St, Springfield"
    assert result.doc.client_name == "Dana Whitfield"
    assert result.doc.doc_date == "March 3, 2026"
    assert result.doc.parse_error is None


def test_source_text_is_carried_through_verbatim():
    """validate.py checks containment against this exact string. Anything the adapter
    does to it here is a change to what 'grounded' means."""
    result, _ = extract(payload())
    assert result.doc.source_text == SOURCE


def test_nulls_survive_as_nulls():
    result, _ = extract(payload(client_name=None, doc_date=None))
    assert result.doc.client_name is None
    assert result.doc.doc_date is None
    assert result.doc.parse_error is None


def test_an_ungrounded_value_is_reported_and_left_alone():
    """The spike's trap-2 failure, arriving at the adapter. The value is not in the
    source; the adapter says so and changes nothing, so validate.py flags
    UNGROUNDED_FIELD rather than the softer NULL_FIELD."""
    result, _ = extract(payload(client_name="the buyer's sister (name not given)"))
    assert result.doc.client_name == "the buyer's sister (name not given)"
    assert result.span_check["client_name"] == "ungrounded"
    assert result.ungrounded_fields() == ["client_name"]


def test_a_grounded_value_is_reported_grounded_under_the_gates_own_normalizer():
    """Punctuation and case differ from the source; the gate would accept it, so the
    adapter's report has to agree. It uses validate._normalize for exactly that."""
    result, _ = extract(payload(property_address="12 oak st.,  Springfield"))
    assert result.span_check["property_address"] == "grounded"
    assert result.ungrounded_fields() == []


def test_the_uncertain_list_is_surfaced_not_discarded():
    notes = ["closing date referenced as 'end of the month' with no date given"]
    result, _ = extract(payload(doc_date=None, uncertain=notes))
    assert result.uncertain == notes


def test_extract_document_returns_only_the_doc():
    """filing.handle_inbound_pdf awaits this and expects an ExtractedDoc. The extras
    are a second function, so wiring a richer surface did not change the contract the
    pipeline was already written against."""
    client = FakeClient(payload())
    doc = run(llm.extract_text(SOURCE, "msg-1", "addendum.pdf", client=client))
    assert isinstance(doc, ExtractedDoc)


def test_a_fenced_or_prefixed_response_is_tolerated():
    """The prompt forbids both. An adapter that dies on a stray ``` fails a document
    for a formatting slip, which is a flag the human cannot act on."""
    result, _ = extract("Here you go:\n```json\n" + payload() + "\n```")
    assert result.doc.doc_type == "addendum"


# --------------------------------------------------------------------------
# 3. Failure is a flag, not an exception
# --------------------------------------------------------------------------

def _blank_pdf() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_a_pdf_with_no_text_layer_flags_and_never_reaches_the_model():
    """A scan. There is nothing to ground anything against, so sending it would ask
    the model to read an image it was not given and invent the answer."""
    client = FakeClient(payload())
    result = run(llm.extract_document_verbose(_blank_pdf(), "msg-1", "scan.pdf", client=client))
    assert result.doc.parse_error is not None
    assert "text layer" in result.doc.parse_error
    assert client.messages.calls == []


def test_unreadable_bytes_flag_rather_than_raise():
    client = FakeClient(payload())
    result = run(llm.extract_document_verbose(b"not a pdf at all", "msg-1", "x.pdf", client=client))
    assert result.doc.parse_error is not None
    assert client.messages.calls == []


def test_an_api_failure_becomes_a_parse_error_not_an_exception():
    """It escapes upstream of store.record_decision otherwise — no flag, no audit row,
    no queue copy. That is finding L5.2's shape, on the extraction side of the gate."""
    result, _ = extract(error=RuntimeError("connection reset"))
    assert result.doc.parse_error is not None
    assert "connection reset" in result.doc.parse_error
    assert result.doc.property_address is None


def test_an_unparseable_response_becomes_a_parse_error():
    result, _ = extract("I'm afraid I can't help with that.")
    assert result.doc.parse_error is not None


def test_the_parse_error_is_bounded_by_the_disclosure_budget():
    """P2.9's rule, applied here: an error string that quotes the text it choked on
    would put the document back into the audit row through the one field redaction
    does not reach."""
    result, _ = extract(error=RuntimeError("x" * 5000))
    assert len(result.doc.parse_error) <= PREVIEW_CHARS + llm.ERROR_PREFIX_BUDGET


def test_a_truncated_response_is_a_failure_not_a_partial_answer():
    """stop_reason == max_tokens means the JSON is cut off; anything salvaged from it
    is a partial extraction presented as a whole one."""
    client = FakeClient(payload())
    client.messages._text = payload()

    class Truncated(FakeResponse):
        def __init__(self, text):
            super().__init__(text)
            self.stop_reason = "max_tokens"

    async def create(**kwargs):
        return Truncated(payload())

    client.messages.create = create
    result = run(llm.extract_text_verbose(SOURCE, "m", "f.pdf", client=client))
    assert result.doc.parse_error is not None
    assert "max_tokens" in result.doc.parse_error


def test_a_missing_api_key_is_reported_before_a_client_is_built(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as exc:
        llm.build_client()
    assert "ANTHROPIC_API_KEY" in str(exc.value)
