"""triage.py — morning brief pipeline: categorize, draft, surface in our own queue.

This pipeline holds gmail.readonly (app/scopes.py, D1). It cannot write to Gmail
at all — no draft creation, no send. Drafts are D3 structured blocks stored in our
own DB and shown in the review queue: grounded facts from the thread, a reply
skeleton of copy-paste lines, explicit [CONFIRM: ...] placeholders for anything not
present in thread_context, and a send-checklist. The human composes and sends from
their own Gmail; this service is never in that loop.

Delivering the brief itself is D2's separate sender process — a different
credential, gmail.send only, recipients from policy.yaml, no LLM, no inbound
access. Not built, and not importable from here when it is.
"""
from app.adapters import gmail, llm

async def build_morning_brief(policy) -> list[dict]:
    items = []
    # TODO(cursor): fetch new + stale threads, categorize per policy.triage_categories,
    # build the D3 structured block per item, persist to the drafts table (queued as
    # N5), and render the brief as plain text: counts + one-line summaries, no links,
    # no buttons — "N items are waiting in your review queue."
    return items
