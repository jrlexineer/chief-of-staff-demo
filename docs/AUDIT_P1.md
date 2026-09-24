## L1 — NO SEND CAPABILITY: **STRUCTURALLY UNENFORCED**

**1. The entire law rests on a false claim about `gmail.compose`.**
`app/adapters/gmail.py:7-8` — *"No send scope, ever. The compose scope cannot put mail on the wire."*
`README.md:30-32` — *"a send scope is never requested, so the credential is technically incapable of sending."*

This is factually wrong. `https://www.googleapis.com/auth/gmail.compose` is documented by Google as *"Create, read, update, and delete drafts. **Send messages and drafts.**"* It is an accepted authorization scope for both `users.messages.send` and `users.drafts.send`. The credential this service requests **is** capable of putting mail on the wire. A future adapter implementer adds three lines inside `gmail.py` — `service.users().messages().send(...)` — and the existing OAuth grant authorizes it with no consent-screen change, no scope change, no core-file edit. L1 has no structural enforcement whatsoever; it has a comment asserting a property the chosen scope does not have.

**2. The product requires an outbound email that no draft can satisfy.**
`policy/policy.yaml:31-33` (`briefing.delivery: email`, `send_hour_local: "07:30"`), `.env.example:6` (`BRIEF_RECIPIENTS=broker@example.com`), `README.md:41-42` (*"Daily interface = the brief email itself"*), `app/pipeline/triage.py:17-19` (TODO: *"Render the brief email as plain text"*).

The morning brief is specified as an email delivered to a recipient list at a fixed hour. A draft sitting in Drafts is not delivered mail. The only way to ship the specified feature is a send path. The design law and the product spec are in direct contradiction, and the spec is the one with an env var and a schedule behind it — an implementer resolving that contradiction will add sending, and `gmail.compose` will let them.

**3. `grep -r 'send' app/` is prescribed as the standing verification** (`README.md:65-67`, `app/models.py:71`). It is a text search for a token, not a capability check. `messages().send()`, `drafts().send()`, or any SMTP/`smtplib` path would be caught only by the literal string; a wrapper named `dispatch`/`deliver`/`transmit` defeats it entirely. Enforcement by grep is not enforcement.

## L2 — GROUNDED DRAFTING: **STRUCTURALLY UNENFORCED**

`app/adapters/llm.py:19-49` is a well-specified prompt and **the only enforcement that exists**. The target state requires invented substance be impossible *by construction*; here it is impossible only if the model complies with instructions.

- No post-generation validation of `draft_reply`'s output anywhere (`app/adapters/llm.py:51-60` returns a raw string; `app/pipeline/triage.py:16` feeds it straight to `gmail.create_draft`). Compare the document path, which has `validate.py` checking the model's claims against `source_text`. The drafting path has no equivalent — no containment check of asserted facts against `thread_context`, no check that a `[CONFIRM:]` placeholder was emitted when substance is absent, no numeric-token check.
- `DRAFTING_PROMPT` lives **inside the adapter stub**, which the phase brief says is the file the implementer is about to rewrite. Nothing outside `llm.py` references it. Adversarially: as the implementer of adapter `llm`, I violate L2 by editing the string literal on line 19 — `validate.py` and `main.py` are untouched, and no test fails.
- **Zero test coverage on `llm.py`.** `tests/test_validate.py` is the only test file; `python -m pytest tests/` → 6 passed, all against `route_document`. No test asserts `DRAFTING_PROMPT` contains the `[CONFIRM:]` clause or the (a)/(b)/(c) restriction.
- `app/adapters/llm.py:52-57` instructs a future maintainer not to post-process `[CONFIRM:]` away. Nothing prevents `triage.py` from doing exactly that.

## L3 — FAIL-CLOSED ROUTING: **FAIL** (gate logic correct; three fail-open/crash paths)

The gate sequence in `app/pipeline/validate.py:45-103` is sound: deterministic, no fuzzy match, no confidence input, exactly-one-folder required. `FlagReason` has six members and `validate.py` uses all six (`:47, :61, :69, :77, :86, :91`) — no unused reason codes. Defects:

**1. The grounding gate is switchable off from config** — `validate.py:53`, `if policy.grounding_required:`. Set `routing.grounding_required: false` in `policy.yaml:13` and the gate vanishes with nothing compensating. Verified:
```
mutated grounding_required -> False | hash unchanged: ab42c9781986
route with grounding off: Route.FILE folder_oak
```
A document whose `source_text` was `'totally unrelated text'` filed to `folder_oak`. L3 lists grounding as non-negotiable; the code makes it a boolean toggle. `policy.yaml:12` (`fail_closed: true # informational — the code has no fail-open path`) is false as written — line 13 is that path.

**2. Unknown `doc_type` in `required_fields` crashes instead of flagging** — `validate.py:73`, `policy.required_fields[doc.doc_type]`, an unguarded dict index. `policy_loader.py:18-29` never validates that every entry in `known_doc_types` has a `required_fields` key. Verified:
```
RAISED: KeyError 'lease' -> no flag, no audit row
```
A one-line `policy.yaml` edit adding a doc type to line 19 without adding line 21-24 turns every such document into an unhandled exception in `filing.py:10` — which is *before* `store.record_decision` on line 11. No flag, no audit row, no review-queue copy. That is fail-open into silence, and it also breaches L5.

**3. Contract mismatch on `known_folders` keys** — `drive.py:5` returns *"{folder display name (address — client): folder_id}"*; `validate.py:37` expects *"normalized address -> drive folder id"*. `_resolve_folder` (`validate.py:106-114`) does normalized **equality**, so a display-name key never matches. Verified: with key `'12 Oak St, Springfield — Dana Whitfield'`, a fully-grounded correct document returns `Route.FLAG / NO_MATCHING_FOLDER`. Fails safe, but the system flags 100% of documents on first integration and the two files disagree about the interface.

**4. `doc_type` is ungrounded.** `ROUTING_FIELDS = ("property_address", "client_name")` (`validate.py:33`) excludes `doc_type`, which selects the required-field set (`:73`) and is embedded in the filename written to Drive (`_canonical_name`, `:121-124`). A hallucinated `doc_type` that happens to be in `known_doc_types` passes every gate. It is a routing field by function but not by definition.

## L4 — POLICY IMMUTABILITY: **STRUCTURALLY UNENFORCED**

No endpoint accepts policy parameters (`main.py:14-31`: `/health`, `/queue`, `/queue/{id}/resolve`, `/stats` — none take policy input). That part holds. What does not:

- **`Policy` is a mutable Pydantic model** — `policy_loader.py:9-16`, no `model_config = ConfigDict(frozen=True)`. Verified above: `p.grounding_required = False` succeeds at runtime.
- **`policy_hash` is a field on the same mutable object** (`policy_loader.py:16`, computed `:28`). Mutate any rule and the hash does not change. `filing.py:11` then stamps `policy.policy_hash` onto the audit row — the audit attests to a ruleset that was **not** the one applied. This inverts the purpose of the hash: it is now a false attestation rather than a tamper-evident one.
- **Policy is a per-call parameter, not a singleton** — `route_document(doc, known_folders, policy)` (`validate.py:36-40`) and `process_document(..., policy)` (`filing.py:7`, untyped). Any future caller constructs `Policy(grounding_required=False, policy_hash="ab42c9781986", ...)` by hand and routes documents through with a forged hash, touching neither `validate.py` nor `main.py`.
- `policy.yaml:5-7` claims *"no client can override anything here"*. True at the HTTP boundary only; the in-process object is wide open.

## L5 — AUDIT COMPLETENESS: **FAIL**

**1. Full document text is persisted.** `store.py:25`, `d.model_dump_json()` serializes the whole `RoutingDecision`, which embeds `ExtractedDoc.source_text` — the complete text layer of the contract. Verified payload:
```json
{"route":"flag","doc":{...,"source_text":"CONFIDENTIAL PURCHASE AGREEMENT ... SSN 123-45-6789 ... price $1,200,000",...}}
```
L5 forbids persisting full bodies. Whether a contract's text layer is technically an "email body," this stores complete legal-document contents — including PII and deal terms — indefinitely in an append-only table. `data.sqlite` sits at repo root (`store.py:8`) and **there is no `.gitignore` in the repository**, so the file is committable.

**2. The audit write is not coupled to the decision.** `route_document` is pure and returns a decision; `store.record_decision` is a separate voluntary call made in exactly one place (`filing.py:11`). Any second consumer of `route_document` — a batch re-file, a dark-launch script, the CLI the README's step 3 describes — produces routing decisions with no audit row. Nothing structural forces the write.

**3. The triage pipeline writes no audit rows at all.** `triage.py:1-20` imports `gmail` and `llm` only; `store` is never imported. Category assignment (`policy.triage_categories`) is a routing decision and drafts are created artifacts — neither is audited. No policy hash is stamped on anything the triage side does.

**4. The "append-only" table is updated in place.** `store.py:2` claims append-only; `store.py:41` is `UPDATE audit SET human_correction=? WHERE id=?`. The pre-correction state is destroyed, and the correction records no actor and no timestamp — so the "free training data" (`store.py:38-39`) cannot be attributed or ordered.

**5. `/queue/{audit_id}/resolve` validates nothing** — `main.py:23-26`. It accepts any `audit_id`, including a `route='file'` row or a nonexistent id, and returns `{"resolved": id}` unconditionally; `store.resolve_flag` checks no rowcount. It also writes a correction onto an already-resolved row silently. Separately, `correction: str` as a bare scalar makes it a **query parameter** under FastAPI, so human corrections travel in the URL and land in access logs.

## L6 — MINIMAL SCOPES: **STRUCTURALLY UNENFORCED**

There is no scope declaration anywhere in the code. `grep -rni 'scope'` returns three hits, all in `gmail.py` docstrings (`:3`, `:7`, `:22`). No `SCOPES` constant, no assertion, no test. The scope list is chosen at OAuth-client-construction time inside the adapter stub the implementer is about to write — the law is a comment adjacent to the file where it will be decided. Compounded by the L1 finding: even the *intended* two-scope list already includes send capability, so "minimal scopes" as documented does not deliver the property it exists to deliver.

---

### (a) Comment/code drift

| Location | Claim | Reality |
|---|---|---|
| `gmail.py:7-8`, `README.md:30-32` | "compose scope cannot put mail on the wire" / "technically incapable of sending" | `gmail.compose` authorizes `messages.send` and `drafts.send` |
| `policy.yaml:12` | "the code has no fail-open path" | `validate.py:53` is a fail-open toggle |
| `policy.yaml:5-7` | "no client can override anything here" | true for HTTP only; `Policy` is mutable in-process |
| `store.py:2` | "append-only audit log" | `store.py:41` does `UPDATE` |
| `models.py:70-71`, `README.md:65-67` | `grep -r 'send' app/` is a sufficient check | greps a token, not a capability |
| `drive.py:5` vs `validate.py:37` | folder keys = "address — client" vs "normalized address" | mismatched; every match fails |
| `validate.py:41-42` | "Pure function… No I/O, no model calls" | accurate, but `:73` can raise `KeyError`, so it is not total |
| `test_validate.py:1` | "Five cases, one per exit" | six tests, six exits; two cover the same exit (`UNGROUNDED_FIELD`), and `PARSE_ERROR` / `UNKNOWN_DOC_TYPE` have **no test** |
| `main.py:2-3` | "Enforcement by absence" | absence today is not enforcement tomorrow; nothing prevents adding the endpoint |

### (b) Dead code and stale keys

- `models.py:67-78` **`TriageDraft`** — zero references outside its definition. The triage pipeline returns `list[dict]` (`triage.py:13`).
- `models.py:56-64` **`AuditRecord`** — zero references. `store.py` hand-rolls the SQL and never constructs it, so the typed "one row per decision, policy_hash stamped" contract is documentation, not code.
- `store.py:6` — **`Route` imported and never used**.
- `policy.yaml:12` `routing.fail_closed` — never read by `policy_loader.py`.
- `policy.yaml:27` `triage.never_auto_send` — never read. A policy key asserting the codebase's central safety property that no code consults.
- `policy.yaml:31-33` `briefing.delivery` / `send_hour_local` — never read; both imply the send path L1 forbids.
- `.env.example:6` `BRIEF_RECIPIENTS` — referenced nowhere; presupposes outbound delivery.
- `filing.py:7` `process_document` and `triage.py:13` `build_morning_brief` — **no callers**. No endpoint, no scheduler, no CLI. Both pipelines are unreachable from `main.py`; the only live surfaces are the queue/stats readers.
- `Evidence.gates_passed` (`models.py:43`) is populated only on the FILE path (`validate.py:101`) and is a hardcoded literal, not accumulated as gates actually execute — it records intent, not evidence.

### (c) Single highest-risk gap

**`gmail.compose` grants send, so L1 — the load-bearing law — is enforced by nothing but a factually incorrect comment.**

Every other safeguard is downstream of this one. The architecture's defensibility to the real-estate team is *"the credential physically cannot send"* (`README.md:27-32`), and that statement is false. The requested OAuth grant already carries `messages.send` authority. Meanwhile `policy.yaml:31-33` and `.env.example:6` commit the product to delivering a scheduled brief **email**, which cannot be built without exercising that authority — so the pressure to use it is not hypothetical, it is on the roadmap at `README.md:51-52`. An implementer wiring `gmail.py` will find the send call already authorized, already needed for the brief, and guarded only by a docstring claiming it is impossible. No core file gets modified; no test breaks; the grep in the README still returns "only statements that sending is forbidden" if the method is reached through the API client rather than spelled out.

The stated law needs a real mechanism — a narrower scope for drafting plus a separate, explicitly-scoped transport for the brief, or an assertion at client construction that the granted scope set excludes send authority — not a comment asserting a property the scope does not have.
