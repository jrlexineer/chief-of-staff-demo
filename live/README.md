# live/

Configuration for `python -m app.run_once`. One file, and it is ground truth.

## known_folders.json

Copy `known_folders.example.json` to `known_folders.json` (gitignored — it holds real
transaction addresses) and fill it in. It stands in for
`drive.list_transaction_folders()`, which is still a stub.

```json
{
  "12 oak st springfield": "1AbCdEfGhIjKlMnOpQrStUv"
}
```

**Keys are normalized addresses, not folder display names.** Normalized means
lowercase, no periods, no commas, collapsed to single spaces — the form
`validate._normalize` produces. `run_once` refuses a key that is not already in that
shape and prints the corrected version rather than fixing it silently: two
normalizations that agree until they don't is exactly the drift audit findings L3.3
and A.6 describe.

Gate 3 is an **exact** match after normalization. `"12 Oak Street"` in the document
will not resolve `"12 oak st springfield"`, and it is meant not to — loosening the
resolver to a fuzzy match is a policy change plus an eval run, never an inline tweak.
The cost of strictness is a flag; the cost of guessing is a misfiled contract nobody
notices.

**Values are Google Drive folder ids.** Nothing in this session writes to Drive — no
Drive credential exists and `app/adapters/drive.py` is a stub — so a placeholder
string is fine for a first run. The id only ever appears in the `WOULD-FILE` line and
in the audit row.
