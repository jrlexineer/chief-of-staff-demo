# SETUP_LIVE.md — running one real email through the pipeline

Everything you have to do on your side, in order. Roughly 20 minutes, most of it
waiting on Google Cloud console pages.

**What this gets you:** `python -m app.run_once` authenticates to your Gmail with a
read-only credential, pulls the newest message carrying a PDF, extracts filing fields
with Claude, routes the result through the real gates, writes an audit row, and prints
the decision.

**What it cannot do.** It holds `gmail.readonly` and nothing else, so it cannot send
mail, create a draft, label, archive, or modify anything in your mailbox — and it does
not merely decline to, it lacks the authority. `app/adapters/gmail.py` refuses to
construct a client at all if Google grants any other scope. There is no Drive
credential in this project, so a "file it" decision prints a `WOULD-FILE` line instead
of uploading. The only thing the program writes is a row in a local sqlite file.

---

## 0. Prerequisites

```powershell
cd C:\Users\josh\Desktop\chief-of-staff
git checkout live-wire
python -m pip install -r requirements.txt
python -m pytest -q
```

The suite should report **126 passed, 1 xfailed**. The xfail is expected — it is the
Drive adapter contract test, which stays xfail until `drive.py` is built.

---

## 1. Anthropic API key

From <https://console.anthropic.com/settings/keys>.

```powershell
# this terminal only
$env:ANTHROPIC_API_KEY = "sk-ant-..."

# or persisted for future terminals (then open a NEW terminal — setx does not
# affect the one you type it in)
setx ANTHROPIC_API_KEY "sk-ant-..."
```

One run is one API call to `claude-sonnet-4-6` with `max_tokens=1000`. Fractions of a
cent.

---

## 2. Google Cloud project

<https://console.cloud.google.com/>

1. Project dropdown, top left → **New project**.
2. Name it something you will recognise later — `chief-of-staff-live` — no
   organization needed. **Create**, then make sure the project picker has switched to
   it. (It usually does; if a later page looks empty, this is why.)

## 3. Enable the Gmail API

1. **APIs & Services → Library**, or go straight to
   <https://console.cloud.google.com/apis/library/gmail.googleapis.com>.
2. Confirm the project name in the header is the one you just made.
3. **Enable**. Takes a few seconds.

## 4. Configure the OAuth consent screen

**APIs & Services → OAuth consent screen.** The console reorganised this into a
"Branding / Audience / Data Access / Verification" set of tabs during 2025; if your
console still shows the older single wizard, the same fields appear in the same order.

1. **User type / Audience: External.** (Internal exists only for Workspace
   organizations. External is correct for a personal `@gmail.com` account and does not
   mean anything is public.)
2. **App name:** `Chief of Staff (local)`. **User support email** and **Developer
   contact email:** your own address. Nothing else on this page matters.
3. **Scopes / Data access:** you can leave this **empty**. The scope is requested by
   the code at authorization time, and `app/scopes.py` is the single place it is
   declared. Adding it here too gives you a second copy to keep in sync, which is the
   drift this project exists to remove. If the console insists on at least one, add
   `.../auth/gmail.readonly` and nothing else.
4. **Audience → Test users → Add users:** add **your own Gmail address**. This is the
   step people skip. An External app in "Testing" mode will only authorize accounts on
   this list; without it you get `Error 403: access_denied` at consent time and it
   reads like a permissions bug.
5. Leave the publishing status as **Testing**. Do not click "Publish app" — an
   unverified published app requesting a Gmail scope goes into verification review,
   which you do not want and do not need.

> Tokens issued by an app in Testing mode expire after **7 days**. When that happens,
> delete `token.json` and run the command again; you will re-consent and get a fresh
> one. That is the expected maintenance cost of not publishing.

## 5. Create the OAuth client

1. **APIs & Services → Credentials → + Create credentials → OAuth client ID**.
2. **Application type: Desktop app.** This matters — a "Web application" client has no
   loopback redirect and `run_local_server()` will fail against it.
3. Name it anything. **Create**.
4. In the dialog that appears, **Download JSON**.
5. Save it as exactly:

   ```
   C:\Users\josh\Desktop\chief-of-staff\credentials.json
   ```

   It is gitignored. It is a client secret, not a password — but keep it off email and
   out of chat anyway.

---

## 6. Define your known folders

This is the ground-truth registry the routing gate resolves against. It stands in for
`drive.list_transaction_folders()`, which is still a stub.

```powershell
copy live\known_folders.example.json live\known_folders.json
notepad live\known_folders.json
```

```json
{
  "12 oak st springfield": "REPLACE_WITH_DRIVE_FOLDER_ID"
}
```

**The keys are normalized addresses: lowercase, no periods, no commas, single spaces.**
Not folder display names. If you write `"12 Oak St., Springfield"` the run refuses and
prints the corrected key for you to paste back — it will not fix it silently, because
two normalizations that agree until they don't is the exact bug audit findings L3.3
and A.6 describe.

The match is **exact** after normalization. `"12 Oak Street"` in a document will not
resolve `"12 oak st springfield"`; it will flag `NO_MATCHING_FOLDER`, and that is
correct behaviour rather than a bug to loosen.

**The values do not have to be real.** Nothing writes to Drive. The id only appears in
the `WOULD-FILE` line and in the audit row, so `folder_placeholder_1` is fine for a
first run.

Put in one or two addresses you actually have documents for — see step 8 for how to
make the run land on a `FILE` decision rather than a flag.

---

## 7. Run it

```powershell
python -m app.run_once
```

The first run opens a browser. Expect, in order:

1. **Account chooser** — pick the address you added as a test user.
2. **"Google hasn't verified this app"** — this is the Testing-mode warning, and it is
   expected. **Advanced → Go to Chief of Staff (local) (unsafe)**.
3. **A single consent line: "Read all resource metadata, and see your email messages
   and settings."** If you are offered anything about *sending* or *composing*, stop —
   something is wrong with the client, not with the code. Deny it and re-check step 5.
4. "The authentication flow has completed" — close the tab.

A `token.json` appears at the repo root (gitignored). Later runs reuse it and do not
open a browser.

### Useful flags

```powershell
# scan the whole inbox, not just messages with a PDF
python -m app.run_once --query "" --scan 25

# a specific sender or thread
python -m app.run_once --query "from:dana@example.com has:attachment"

# headless / no browser: prints the consent URL to paste
python -m app.run_once --no-browser

# keep the audit database somewhere else
python -m app.run_once --db .\scratch.sqlite
```

---

## 8. Reading the output

Eight sections. The two that matter:

**Section 5, extraction.** The four extracted fields, the model's own uncertainty
notes, and a grounding pre-check per routing field (`grounded` / `ungrounded` /
`null`). The pre-check is a *prediction* of what the gate will decide — the adapter
reports what it found and never edits it, so a value the model invented arrives at the
gate intact and is flagged as invented rather than quietly nulled.

**Section 6/7, the decision.** `FILE` prints `WOULD-FILE` with the canonical filename
and the folder id. `FLAG` prints the reason and the detail. Then section 8 prints the
audit row exactly as it was persisted — note that it carries a sha256 and a 200-char
preview where the document text would be, never the text itself.

### Getting a FILE rather than a FLAG

A flag is the normal outcome and is not a failure. To see the whole path, all of these
have to line up:

- the PDF has a real text layer (a scan flags `parse_error` and never reaches the
  model — deliberately, since there is nothing to ground against);
- the document's type is one of `contract, addendum, disclosure, amendment`;
- its property address and client name appear **in the document's own text**;
- that address, normalized, is a key in `live/known_folders.json`.

The most reliable way to get there on the first try: open one of your own PDFs, read
the property address off it, and paste the normalized form into
`live/known_folders.json`.

---

## Troubleshooting

| What you see | What it is |
|---|---|
| `Error 403: access_denied` at consent | Your address is not in **Test users** (step 4.4). |
| `Refusing to construct a Gmail client. Granted scopes …` | Google granted something other than exactly `gmail.readonly`. This is the safety mechanism working. Delete `token.json`, check the consent screen scope list has nothing extra, re-run. |
| `RuntimeError: … different scope set than was requested` | Same cause, caught one layer earlier by oauthlib. |
| `FileNotFoundError: No OAuth client secrets at …` | `credentials.json` is missing or misnamed (step 5.5). |
| `invalid_grant` / `Token has been expired or revoked` | Testing-mode tokens last 7 days. Delete `token.json` and re-run. |
| `redirect_uri_mismatch` | The OAuth client is a **Web application**, not a **Desktop app**. Make a new one (step 5.2). |
| `ANTHROPIC_API_KEY is not set` | Step 1. If you used `setx`, open a new terminal. |
| `REFUSED: … keys must already be normalized addresses` | Copy the corrected keys the message prints into `live/known_folders.json`. |
| `no PDF attachment in the N most recent matching messages` | Raise `--scan`, or use `--query ""` to fall back to a body extraction. |

---

## What is deliberately not here

- **No Drive write path.** No service-account credential exists and `drive.py` is a
  stub. `WOULD-FILE` is the honest output, not a placeholder for something almost
  working.
- **No send path, ever.** See `app/scopes.py` — D1 (split credentials), D2 (outbound
  delivery is a separate future `sender/` process), D4 (`gmail.compose` is banned by
  name because Google documents it as granting send).
- **No drafting.** `llm.draft_reply` is still a stub. D3 says a draft is structured
  blocks, not prose; wiring it to free text would build the thing D3 decided against.
  Queued as N5.
