"""drive.py — STUB. Service account on the shared drive.
The folder tree under 'Active Transactions' is the ground-truth registry.

CONTRACT (audit findings L3.3, A.6). The keys of the dict this module returns are
NORMALIZED ADDRESSES, and this module owns the normalization: it must export a
`normalize_address()` that `validate._resolve_folder` uses as its own normalizer, so
there is one function rather than two that agree until they don't.

Returning display names — "12 Oak St, Springfield — Whitfield" — is a contract
violation, not a formatting preference. The resolver compares keys after
normalization, so a display-name key matches nothing: every document flags
NO_MATCHING_FOLDER, on the first run, forever, and it reads like an extraction
problem rather than a disagreement between two files. Pinned by
tests/test_folder_contract.py, where the shared-function assertion is xfail(strict)
until this stub is built."""

async def list_transaction_folders() -> dict[str, str]:
    """Returns {normalize_address(folder address): folder_id}. See the contract above:
    NOT the folder's display name."""
    raise NotImplementedError

async def file_document(folder_id: str, name: str, pdf_bytes: bytes) -> str:
    raise NotImplementedError

async def file_to_review_queue(name: str, pdf_bytes: bytes) -> str:
    """The 'Needs review' folder — flagged docs are still SAVED, just not sorted."""
    raise NotImplementedError
