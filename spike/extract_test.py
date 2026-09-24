"""extract_test.py -- SPIKE. Does a single extraction prompt pull structured facts
out of messy brokerage email, without inventing any of them?

Not wired into app/. Reads .txt files from spike/samples/, sends each through one
Anthropic call, and prints what came back. The thing being measured is not recall --
it is whether the model puts what it does not know into anything_uncertain instead of
guessing. Empty lists are correct answers.

Each result is also written to spike/samples/out_<name>.json, because the table only
shows counts and the counts are not the finding. Grading a trap means reading the
values back against the source email.

    python spike/extract_test.py
"""

import json
import os
import sys
from pathlib import Path

SAMPLES_DIR = Path(__file__).parent / "samples"

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1000

# The eight content fields, in the order the prompt lists them. anything_uncertain is
# deliberately not in here: it is the escape hatch, not a finding.
FIELDS = [
    "people",
    "orgs",
    "dates_or_deadlines",
    "amounts",
    "property_refs",
    "doc_mentions",
    "asks_or_questions",
    "commitments_made",
]

EXTRACTION_PROMPT = """\
You extract facts from a residential real-estate brokerage email. You are a \
transcriber, not an analyst.

Return STRICT JSON only. No prose before or after it, no markdown fences, no \
explanation. Exactly these keys, all of them present, every value a list:

{
  "people": [],
  "orgs": [],
  "dates_or_deadlines": [],
  "amounts": [],
  "property_refs": [],
  "doc_mentions": [],
  "asks_or_questions": [],
  "commitments_made": [],
  "anything_uncertain": []
}

Rules:

- A value belongs in one of the eight content fields ONLY if it is EXPLICITLY present \
in the email text. Quote or near-quote it. Do not normalize a partial address into a \
full one, do not resolve "next Friday" into a date, do not convert "about what the \
Hartley place went for" into a number.
- Anything you would have to infer, guess, complete, or look up goes in \
anything_uncertain instead, as a short string naming what is missing and why. For \
example: "budget implied by 'nothing over the Kensington range' but no figure stated".
- Never put an inferred value in a content field and also note it as uncertain. It goes \
in one place: anything_uncertain.
- Empty lists are correct answers. An email with no deadlines has \
"dates_or_deadlines": []. Do not pad.
- asks_or_questions covers what the sender wants FROM the recipient, including \
questions buried mid-paragraph or phrased as statements ("I'd love to know your read \
on the inspection"). commitments_made covers what the SENDER has promised to do.
- In a forwarded or quoted thread, extract from the whole thing, but if it is unclear \
who said what, say so in anything_uncertain rather than attributing.
- If the email contains nothing relevant -- marketing, a newsletter, an automated \
notice -- return all nine keys with empty lists, except anything_uncertain, which may \
note that the message carries no client content.

Email follows.

---
{email_text}
---

JSON only."""


SETUP_INSTRUCTIONS = """\
ANTHROPIC_API_KEY is not set, so there is nothing to call. Set it and re-run.

  PowerShell, this terminal only:
      $env:ANTHROPIC_API_KEY = "sk-ant-..."

  PowerShell, saved for future terminals:
      setx ANTHROPIC_API_KEY "sk-ant-..."
      (then open a NEW terminal -- setx does not affect the current one)

  bash / git-bash, this terminal only:
      export ANTHROPIC_API_KEY="sk-ant-..."

A key comes from https://console.anthropic.com/settings/keys. This spike sends the
contents of spike/samples/*.txt to the API, so put only sanitized text in there.
"""


def parse_response(text):
    """Pull a JSON object out of a model response. Tolerates fenced or prefixed output
    -- the prompt forbids both, but a spike that dies on a stray ``` measures the wrong
    thing. Raises ValueError if there is no parseable object."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[: -len("```")]
    start, end = body.find("{"), body.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object in response")
    parsed = json.loads(body[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("response JSON was not an object")
    return parsed


def coerce(parsed):
    """Normalize to {field: list}. A missing key is an empty list; a scalar where a list
    was asked for is wrapped, and that itself is worth seeing in the output."""
    out = {}
    for key in FIELDS + ["anything_uncertain"]:
        value = parsed.get(key, [])
        if value is None:
            value = []
        elif not isinstance(value, list):
            value = [value]
        out[key] = value
    return out


def extract(client, email_text):
    import anthropic

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[
                {
                    "role": "user",
                    "content": EXTRACTION_PROMPT.replace("{email_text}", email_text),
                }
            ],
        )
    except anthropic.AuthenticationError:
        raise RuntimeError("auth failed -- check ANTHROPIC_API_KEY")
    except anthropic.RateLimitError:
        raise RuntimeError("rate limited")
    except anthropic.APIStatusError as exc:
        raise RuntimeError("API %s: %s" % (exc.status_code, exc.message))
    except anthropic.APIConnectionError:
        raise RuntimeError("connection failed")

    if response.stop_reason == "max_tokens":
        raise RuntimeError("hit max_tokens (%s) -- response truncated" % MAX_TOKENS)

    text = "".join(b.text for b in response.content if b.type == "text")
    return coerce(parse_response(text))


def write_result(path, payload):
    """Dump one email's result next to it as out_<name>.json. Written on failure too,
    carrying the error -- a missing file should mean the run never reached this email,
    not that it reached it and something went wrong."""
    out_path = path.parent / ("out_%s.json" % path.stem)
    out_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return out_path


def summarize_fields(record):
    """One-line breakdown of which content fields actually got hits."""
    hits = ["%s(%d)" % (f, len(record[f])) for f in FIELDS if record[f]]
    return " ".join(hits) if hits else "-"


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(SETUP_INSTRUCTIONS)
        return 0

    try:
        import anthropic
    except ImportError:
        print("The anthropic package is not installed. Run:\n\n    pip install anthropic\n")
        return 0

    if not SAMPLES_DIR.is_dir():
        print("No samples directory at %s" % SAMPLES_DIR)
        return 0

    paths = sorted(SAMPLES_DIR.glob("*.txt"))
    if not paths:
        print("No .txt files in %s -- nothing to extract." % SAMPLES_DIR)
        return 0

    client = anthropic.Anthropic()

    print("model=%s  max_tokens=%d  samples=%d\n" % (MODEL, MAX_TOKENS, len(paths)))
    header = "%-24s %6s %10s  %s" % ("FILE", "ITEMS", "UNCERTAIN", "FIELDS FOUND")
    print(header)
    print("-" * max(len(header), 78))

    total_items = 0
    total_uncertain = 0
    failures = []

    for path in paths:
        email_text = path.read_text(encoding="utf-8", errors="replace")
        try:
            record = extract(client, email_text)
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            failures.append((path.name, str(exc)))
            write_result(path, {"_source": path.name, "_error": str(exc)})
            print("%-24s %6s %10s  %s" % (path.name[:24], "-", "-", "ERROR: %s" % exc))
            continue

        write_result(path, dict(record, _source=path.name))

        items = sum(len(record[f]) for f in FIELDS)
        uncertain = len(record["anything_uncertain"])
        total_items += items
        total_uncertain += uncertain
        print(
            "%-24s %6d %10d  %s"
            % (path.name[:24], items, uncertain, summarize_fields(record))
        )

    print()
    print(
        "%d emails, %d total fields extracted, %d flagged uncertain."
        % (len(paths), total_items, total_uncertain)
    )
    if failures:
        print("%d failed:" % len(failures))
        for name, reason in failures:
            print("  %s -- %s" % (name, reason))
    print("Full results written to %s\\out_*.json" % SAMPLES_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
