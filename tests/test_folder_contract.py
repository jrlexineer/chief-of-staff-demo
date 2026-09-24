"""test_folder_contract.py — what the keys of known_folders are.

Covers audit findings L3.3 and A.6: `drive.list_transaction_folders()` documented its
return as `{folder display name: id}` while `validate._resolve_folder` matches keys as
normalized addresses. It fails safe — a display name like
"12 Oak St, Springfield — Whitfield" does not equal the normalized address — but it
fails safe on 100% of documents the first time the real adapter is wired in, which
reads as "the model is bad at addresses" rather than "the two sides disagree".

The contract: **keys are normalized addresses, and the Drive adapter owns the
normalization**, via a `normalize_address()` it shares with the resolver. One
function, called on both sides, or the two normalizations drift the same way the
two docstrings did.

The first test passes today and pins the resolver's half. The second is
xfail(strict=True): the adapter is a stub, so it must fail — and it must start
failing-as-passing the moment someone builds the adapter without the shared
function, which is exactly when the reminder is useful.

    python -m pytest tests/test_folder_contract.py
"""
import pytest

from app.adapters import drive
from app.pipeline import validate


def test_the_resolver_matches_normalized_address_keys():
    """The resolver's half of the contract, as it behaves today."""
    folders = {"12 oak st springfield": "folder_oak"}
    assert validate._resolve_folder("12 Oak St., Springfield", folders) == [
        "12 oak st springfield"
    ]


def test_a_display_name_key_is_a_contract_violation_not_a_near_miss():
    """The audit's scenario. Documented here so the failure is recognisable when it
    happens: every document flags NO_MATCHING_FOLDER, and the cause is the key
    format, not the extraction."""
    folders = {"12 Oak St, Springfield — Whitfield": "folder_oak"}
    assert validate._resolve_folder("12 Oak St, Springfield", folders) == []


@pytest.mark.xfail(
    strict=True,
    reason="L3.3: drive.py is a stub. The adapter must expose normalize_address() "
           "and the resolver must use that same function — not a second one that "
           "happens to agree today.",
)
def test_the_adapter_and_the_resolver_share_one_normalization():
    normalize = drive.normalize_address
    assert validate._normalize is normalize
    assert normalize("12 Oak St., Springfield") == normalize("12 oak st  springfield")
