"""filing.py — the document pipeline: extract → validate → act. Fail closed.

The audit write is not this module's responsibility any more.
validate.process_document() performs it, and it is the only public route to a
decision, so a future second consumer of the gate cannot skip it by accident.
"""
from app.adapters import llm, drive
from app.pipeline.validate import process_document
from app.models import Route
from app import store

async def handle_inbound_pdf(pdf_bytes: bytes, message_id: str, filename: str, policy):
    doc = await llm.extract_document(pdf_bytes, message_id, filename)
    folders = await drive.list_transaction_folders()
    decision = process_document(doc, folders, policy, store)

    if decision.route == Route.FILE:
        await drive.file_document(decision.target_folder_id, decision.canonical_name, pdf_bytes)
    else:
        # Flagged docs are still preserved — visible queue, never lost.
        await drive.file_to_review_queue(filename, pdf_bytes)
    return decision
