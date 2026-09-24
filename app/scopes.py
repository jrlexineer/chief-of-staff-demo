"""scopes.py — the send law, expressed as code instead of prose.

This module is the single place in the codebase where Gmail authority is named.
Any OAuth client this service constructs asserts against it before it is used.

    D1 — SPLIT CREDENTIALS. The triage/drafting service holds gmail.readonly and
         nothing else. It is structurally incapable of creating a Gmail draft or
         putting mail on the wire, because the credential it holds does not carry
         that authority — not because a comment says so.

    D2 — THE MORNING BRIEF IS SOMEONE ELSE'S JOB. Delivery belongs to a separate
         tiny sender process (future sender/ directory) with its own OAuth
         credential scoped to gmail.send ONLY, recipients read exclusively from
         policy.yaml, no LLM, and no inbound mail access. It is not built yet, and
         it will not import from this package's triage path when it is.

    D4 — gmail.compose IS PERMANENTLY BANNED. Google documents it as "Create,
         read, update, and delete drafts. Send messages and drafts." It is an
         accepted authorization scope for users.messages.send and
         users.drafts.send. A credential holding gmail.compose CAN send mail. The
         prior claim that "the compose scope cannot put mail on the wire" was
         factually wrong, and it was the only thing standing between this service
         and an outbound path.

assert_safe() is not a blocklist. It requires an exact match against
TRIAGE_SCOPES, so a scope nobody anticipated fails closed too. SEND_CAPABLE_SCOPES
exists to make the refusal legible in the error message, not to define the rule.
"""
from __future__ import annotations

from collections.abc import Iterable

#: The complete set of scopes the triage/drafting service may ever hold.
#: Widening this is a deliberate, reviewed act that breaks tests/test_scopes.py.
TRIAGE_SCOPES: frozenset[str] = frozenset({
    "https://www.googleapis.com/auth/gmail.readonly",
})

#: Scopes that carry send authority, named so a refusal can say why.
#: gmail.compose and gmail.modify both authorize users.drafts.send;
#: mail.google.com is full account access.
SEND_CAPABLE_SCOPES: frozenset[str] = frozenset({
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://mail.google.com/",
})


def assert_safe(granted: Iterable[str]) -> None:
    """Raise RuntimeError unless `granted` is exactly TRIAGE_SCOPES.

    Call this at OAuth client construction, against the scopes the token actually
    came back with — not against the list that was requested. What the credential
    carries is the only thing that matters.
    """
    granted = set(granted)
    if granted == set(TRIAGE_SCOPES):
        return

    send_capable = sorted(granted & SEND_CAPABLE_SCOPES)
    unexpected = sorted(granted - TRIAGE_SCOPES - SEND_CAPABLE_SCOPES)
    missing = sorted(TRIAGE_SCOPES - granted)

    reasons = []
    if send_capable:
        reasons.append(f"grants send authority: {send_capable}")
    if unexpected:
        reasons.append(f"not permitted for this service: {unexpected}")
    if missing:
        reasons.append(f"required but absent: {missing}")

    raise RuntimeError(
        "Refusing to construct a Gmail client. Granted scopes "
        f"{sorted(granted)} != TRIAGE_SCOPES {sorted(TRIAGE_SCOPES)} — "
        + "; ".join(reasons)
        + ". See app/scopes.py (D1, D2, D4)."
    )
