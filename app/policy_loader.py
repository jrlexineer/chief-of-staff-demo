"""policy_loader.py — loads policy.yaml at boot, hashes it, refuses to run without it.

Policy is frozen: once constructed it cannot be mutated in-process, so the ruleset
that a decision was made under is the ruleset the audit row attests to. policy_hash
is DERIVED from the field values, not stored alongside them — change any rule, by
any route, and the hash changes with it. That is what makes it tamper-evident
rather than a decorative string.
"""
from __future__ import annotations
import hashlib, json, yaml
from pathlib import Path
from pydantic import BaseModel, ConfigDict

POLICY_PATH = Path(__file__).parent.parent / "policy" / "policy.yaml"

class Policy(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: int
    known_doc_types: list[str]
    required_fields: dict[str, list[str]]
    triage_categories: list[str]
    waiting_threshold_days: int

    @property
    def policy_hash(self) -> str:
        """Fingerprint of the effective ruleset, not of the file on disk.
        A hand-constructed Policy cannot forge the hash of a different one."""
        payload = json.dumps(self.model_dump(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

def load_policy(path: str | Path | None = None) -> Policy:
    """Load from `path`, or from POLICY_PATH when the caller names none.

    Read at call time, not bound at def time, so POLICY_PATH stays monkeypatchable
    and the composition root can name a policy file explicitly.
    """
    source = Path(path) if path is not None else POLICY_PATH
    data = yaml.safe_load(source.read_bytes())
    known = data["documents"]["known_doc_types"]
    required = data["documents"]["required_fields"]

    # Adding a doc type without adding its required_fields entry (or vice versa)
    # must fail at boot, loudly, rather than at the first document of that type.
    if set(known) != set(required.keys()):
        raise ValueError(
            "policy.yaml: known_doc_types and required_fields must cover exactly "
            "the same doc types; "
            f"missing from required_fields={sorted(set(known) - set(required))}, "
            f"missing from known_doc_types={sorted(set(required) - set(known))}"
        )

    return Policy(
        version=data["version"],
        known_doc_types=known,
        required_fields=required,
        triage_categories=data["triage"]["categories"],
        waiting_threshold_days=data["triage"]["waiting_threshold_days"],
    )
