# Meridian Residential Chief of Staff — Architecture

**One page. Colour-coded honestly.** The demo in `demo/index.html` is a scripted
click-through with hardcoded data — nothing in it calls the system described below.
This document exists so that nobody watching the demo has to guess which parts are
real. Every BUILT claim cites the commit that made it true.

| | Legend |
|---|---|
| ■ **BUILT** | Exists in `app/`, enforced by a mechanism, covered by a test in the passing suite. |
| ◧ **SCAFFOLDED** | Structure exists — a decision, a stub, an endpoint, a schema — but the behaviour is not wired end to end. |
| □ **PLANNED** | Named and specified. No code. |

Suite at time of writing: **69 passed, 1 xfailed** (`python -m pytest -q`).

---

## The four layers

```
   DATA                RETRIEVAL             REASONING              SURFACES
   ────                ─────────             ─────────              ────────
   inbound mail   ──►  per-agent book   ──►  guardrails       ──►   EMAIL (primary)
   documents           (scoped read)                                 briefing in
   listings /                                                        blocks in
   relationship        □ CRM / MLS          ■ frozen policy         review queue
   notes               ◧ Gmail (read)       ■ fail-closed           (secondary)
                       ◧ Drive              ■ audited
                       ■ read-only cred     ◧ LLM adapter     □ email delivery
                                                               ◧ queue surface
                                                               ◧ drafting blocks
                                                               □ sender/

   Hosted single instance · per-agent OAuth, read-only · one consent screen
```

---

## 1 · DATA

| | Component | State |
|---|---|---|
| ■ | **Audit store** — `app/store.py`, SQLite. Every routing decision is persisted. Decision fields are never revised; corrections append to a separate table. | `68dea55` (P2.10) |
| ■ | **Redaction at the storage boundary** — `app/redact.py`. `RedactedDoc` / `RedactedDecision` have no `source_text` field, so the writer *cannot* persist document text. Rows keep a sha256, a 200-char preview, and the true length. A sweep in `init()` redacts rows written before the fix. | `df8ec43` (P2.8) |
| ■ | **Frozen policy** — `policy/policy.yaml` loaded into an immutable Pydantic model; `policy_hash` is derived (sha256 over the dump), not stored, so a mutated ruleset cannot attest to the old hash. | `32193c1` (P2.1) |
| ◧ | **Document extraction** — `ExtractedDoc` model exists and the routing gate consumes it. No real PDF/OCR ingest behind it. | — |
| □ | **Listings, comps, relationship notes** — the entire data layer the demo's briefing is built from. Invented for the demo, in a JS object. Nothing in `app/` models a listing, a comp, or a call note. | — |

## 2 · RETRIEVAL — the per-agent book

| | Component | State |
|---|---|---|
| ■ | **Read-only credential** — `app/scopes.py` pins `TRIAGE_SCOPES` to `gmail.readonly` alone. `assert_safe()` requires an *exact* match, so an unanticipated scope fails closed too; `gmail.compose` is banned by name (D4 — Google documents it as granting send). `tests/test_scopes.py` pins the constant and asserts the rejections. | `0c05c3a` (P2.3), `9ab7e1b` (P2.4) |
| ■ | **Folder-key contract** — the adapter owns `normalize_address()`; the resolver uses that same object rather than a second function that agrees today. The shared-function assertion is `xfail(strict=True)`, so it becomes a hard failure the moment the adapter is built without it. | `bd84aae` (P2.11) |
| ◧ | **Gmail adapter** — `app/adapters/gmail.py` is a read-only stub pointing at `app.scopes`. No fetch, no thread parsing. | — |
| ◧ | **Drive adapter** — `app/adapters/drive.py` states the contract; the adapter itself is a stub. | — |
| □ | **CRM / MLS integration** — the demo's "scoped to this agent's own book" claim. There is no CRM connector, no MLS connector, and no per-agent scoping model. This is the largest single gap between the demo and the system. | — |

## 3 · REASONING — guardrails and brand voice

| | Component | State |
|---|---|---|
| ■ | **Fail-closed routing** — the grounding gate is unconditional. The config toggle that switched it off (`policy.grounding_required`) and the field, YAML key and guard behind it are deleted; a test re-runs the audit's original reproduction. | `32193c1` (P2.1) |
| ■ | **Unknown input flags rather than raises** — an unknown `doc_type` becomes `FLAG/UNKNOWN_DOC_TYPE` with an audit row, instead of a `KeyError` that produced no flag, no row and no queue copy. `load_policy()` rejects a policy whose `known_doc_types` and `required_fields` disagree in either direction. | `66b08f8` (P2.2) |
| ■ | **Audited decisions — the write is coupled to the decision.** The gate is private (`_route_document`); the only public entry always records. A crash inside the gate becomes `FLAG/EXTRACTION_ERROR` rather than a lost document, and a failed audit write propagates, so no caller acts on an unrecorded decision. | `cd78dbe` (P2.6), `f66560f` (P2.9) |
| ■ | **Composition root** — `create_app()` is the only place in the process that constructs a Policy, so a second ruleset in flight is a deliberate act rather than a default argument nobody noticed. | `8760b63` (P2.12) |
| ◧ | **LLM adapter** — `app/adapters/llm.py` is a stub holding a prompt string. No post-generation validation of drafting output exists yet (audit findings L2.1–L2.4). | — |
| □ | **`doc_type` evidence gate (D5)** — per-type marker phrases in policy, at least one of which must appear in the document. Fully specified as queue item N6. No code. | — |
| □ | **"Brand voice"** — deliberately *not* a component. Per **D3**, the system does not imitate the agent's voice; it emits structured blocks. The demo's caption states this. There is no voice model to build. | — |

## 4 · SURFACES

**Email is the primary surface.** Briefings arrive as email and structured blocks
arrive in the same email; the agent never leaves the inbox they already work in.
The review queue is secondary — an operator surface for flagged items, not the
place the product is used. Deployment shape: a **hosted single instance**, with
**per-agent OAuth, read-only** (`app/scopes.py` pins `gmail.readonly` and exact-match
enforces it — `0c05c3a`, P2.3). Onboarding is therefore one authorization screen:
nothing to install, and no second tool to adopt.

| | Component | State |
|---|---|---|
| □ | **Email delivery — the primary surface.** Briefings out, structured blocks out, in one message to the agent's own inbox. This is what the demo depicts and it is **unbuilt**: outbound delivery is D2's `sender/` process, which does not exist. Note the constraint this inherits — the triage service cannot send, so the briefing must be composed by one process and delivered by another with a `gmail.send`-only credential. | — |
| ◧ | **Review queue — secondary.** `GET /queue` and `POST /queue/{audit_id}/resolve` exist in `app/main.py` and are validated (Pydantic body so corrections stay out of access logs; 404 unknown id, 409 non-flag row — checks live in the store, not only the endpoint). But the queue holds *document* flags, not drafts, and no UI renders it. | `68dea55` (P2.10) |
| ■ | **Per-agent read-only authorization.** The scope the whole surface story rests on: one consent screen, `gmail.readonly` only, exact-match enforced, send-capable scopes named and tested as rejected. | `0c05c3a` (P2.3) |
| ◧ | **Drafting blocks (D3)** — the three-block shape the demo renders (grounded facts on screen III; reply skeleton with `[CONFIRM:]` placeholders and send checklist on screen IV) is a **locked decision**, recorded in `app/pipeline/triage.py`. The schema and `drafts` table are queue item N5 — the largest open item, with L2.1–L2.4, L5.3 and B.1 waiting on it. | — |
| □ | **Sender (`sender/`)** — the morning brief. Per **D2**, a separate tiny process with its own `gmail.send`-only credential, recipients read exclusively from `policy.yaml`, no LLM, no inbound access. Not built. Until it is, the product has no way to deliver its headline feature — and nothing in `app/` can send. | — |
| □ | **Pipeline wiring** — both pipelines are still unreachable from `main.py`: no endpoint, no scheduler, no CLI (audit finding B.8). `create_app()` is where that wiring goes. | — |

---

## What the demo asserts that the system does not yet do

Stated plainly, because the demo is persuasive and the gap matters:

1. **The retrieval is fictional.** No listing, comp, or relationship note has ever been
   read by this system. The "your own call notes, not the MLS" line is the product
   thesis, not a running feature.
2. **No timing claim is made anywhere any more.** The briefing carried an
   "Assembled in 4.2s" timestamp, which was never benchmarked — there is no pipeline
   to benchmark. Deleted in DEMO.8 (`GAP_CHECK.md` **U9**): it was the only figure on
   screen with no record behind it, and speed is the one thing this audience already
   has. The wall-clock timestamps that remain (8:12, 9:03, 9:47, 9:51) are the
   scenario’s own clock, not a performance claim.
3. **The three reply blocks are a decision, not a schema.** D3 is locked and the demo
   renders it faithfully — but N5 has not been written.
4. **Screen I's filing is a faithful picture of the gate, not a recording of it
   running.** The four checks it shows are `validate.py`'s four gates in `validate.py`'s
   order, and they are BUILT — unconditional grounding (`32193c1`), typed flags
   (`66b08f8`), coupled audit write (`cd78dbe`), exact folder resolution (`bd84aae`).
   What does not exist is everything either side of them: `app/adapters/drive.py` and
   `app/adapters/gmail.py` both raise `NotImplementedError`, so no PDF has ever reached
   this gate and nothing has ever been written to a Drive folder. `filing.handle_inbound_pdf`
   has no caller (finding B.8). The judgement on screen is real; the pipes are not.
5. **Nothing in the codebase governs the Drive credential the filing screen implies.**
   `app/scopes.py` is a *Gmail* law: `TRIAGE_SCOPES` is `gmail.readonly` and `assert_safe()`
   demands an exact match, which means it would **reject** a credential carrying
   `drive.file` — `tests/test_scopes.py:47-58` asserts exactly that. That is correct under
   D1, and it is also the point: filing needs write authority the triage service may not
   hold, so it needs its own credential and its own declaration, in the shape D2 gives
   `sender/`. Neither exists. Until one does, `SCRIPT.md`'s answer to "does it write to
   Drive?" — *it writes into Active Transactions and nowhere else* — is a design intention
   with no mechanism behind it, which is the exact species of claim `docs/AUDIT_P1.md`
   was written to catch. See `demo/GAP_CHECK.md` (finding **X-1**).
6. **The briefing does not arrive as an email, because nothing can send one.** This is
   the largest gap the email-native framing opens: the demo's entire delivery story is
   D2's `sender/` process, which is unbuilt, and `app/` is structurally incapable of
   sending (L1, D1, D4). The framing is honest about the *shape* — inbox, no portal,
   one read-only consent — and asserts a delivery path that does not exist yet.

## What is real, and is the harder half

The safety substrate. The service **cannot** send mail — not by policy, by credential:
`gmail.readonly` is the only scope it may hold, exact-match enforced, with the
send-capable scopes named and tested as rejected. Policy is immutable with a derived
hash. The grounding gate has no off switch. Every decision that gets made gets
recorded, document text never reaches the audit table, and the record is append-only.
That is 27 of 40 audit findings closed, each fixed test-first against a reproduction of
the original failure.

The demo shows the surface. This layer is why the surface can be trusted.

---

*Detail: `PROJECT_STATUS.md` (findings ledger, decisions D1–D5, queue N5–N8) ·
`docs/AUDIT_P1.md` (authoritative findings list).*
