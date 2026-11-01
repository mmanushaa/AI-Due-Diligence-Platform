"""Celery tasks (step 1.9).

The task is a thin wrapper: all the work and all the status bookkeeping live in
`app.jobs.audit.run_audit`, which can be called directly in tests and from a script.
"""

import uuid

from app.jobs.audit import run_audit
from app.jobs.celery_app import celery_app


@celery_app.task(name="audits.run_audit", acks_late=True, reject_on_worker_lost=True)
def run_audit_task(audit_id: str) -> dict:
    """Run one audit. A redelivered message after a worker crash simply runs it again.

    Re-running is safe: indexing replaces the audit's file rows, and every scanner skips
    fingerprints that are already stored.
    """
    result = run_audit(uuid.UUID(audit_id))
    return {
        "audit_id": str(result.audit_id),
        "status": result.status.value,
        "stage": result.stage.value if result.stage else None,
    }
