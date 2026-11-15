"""Celery tasks (steps 1.9 and 3.2).

Since step 3.2 the task does one thing: hand the audit to the workflow. It starts
the LangGraph run when the audit's thread has no checkpoint and resumes it when one
exists, so a message redelivered after a worker death continues the run at the last
completed node instead of redoing intake, indexing and the scanners.

The stage code lives in ``app.workflow.nodes``; nothing here knows what a stage is.
"""

import uuid

from app.jobs.celery_app import celery_app
from app.workflow.graph import start_or_resume


@celery_app.task(name="audits.run_audit", acks_late=True, reject_on_worker_lost=True)
def run_audit_task(audit_id: str) -> dict:
    """Start or resume the audit's workflow."""
    state = start_or_resume(uuid.UUID(audit_id))
    return {
        "audit_id": str(state.get("audit_id", audit_id)),
        "status": state.get("status"),
        "stage": state.get("stage"),
        "completed": list(state.get("completed") or ()),
    }
