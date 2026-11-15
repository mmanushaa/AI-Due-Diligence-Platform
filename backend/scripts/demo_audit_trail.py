"""Step 3.5 demo: the append-only trail, and a full review flow's log.

Run from the repository root:

    backend/.venv/Scripts/python.exe backend/scripts/demo_audit_trail.py

It builds a small audit with one critical finding, runs the workflow until the review
gate pauses it, approves the review through the real API, then tries - six times - to
rewrite history, and finally prints the audit trail the API serves.
"""

from __future__ import annotations

import sys
import tempfile
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from app.api import reviews as reviews_api  # noqa: E402
from app.db.enums import (  # noqa: E402
    AuditStatus,
    Category,
    EvidenceKind,
    FindingOrigin,
    Severity,
    SourceType,
    VerificationStatus,
    Verdict,
)
from app.db.models import (  # noqa: E402
    Audit,
    AuditLog,
    Evidence,
    Finding,
    Repository,
    Review,
)
from app.db.session import get_engine, get_session_factory  # noqa: E402
from app.main import app  # noqa: E402
from app.workflow.graph import pending_nodes, postgres_checkpointer, start_or_resume  # noqa: E402
from app.workflow.nodes import WorkflowServices  # noqa: E402


def stub_services() -> WorkflowServices:
    """No scanners, no model calls: the graph's shape is what this demo is about."""

    def index(session, audit_id, snapshot):
        return SimpleNamespace(included=1, excluded=0)

    def scanners(audit_id, snapshot, session_factory=None):
        return SimpleNamespace(failed=())

    def agents(session, audit_id):
        return SimpleNamespace(errors=(), accepted=0, calls=0)

    def validate(session, audit_id, semantic=True):
        return SimpleNamespace(checked=0, verified=0, rejected=0, unresolved=0)

    return WorkflowServices(
        session_factory=get_session_factory(),
        index=index,
        scanners=scanners,
        agents=agents,
        validation=validate,
    )


def seed(tmp: Path, session) -> tuple[uuid.UUID, uuid.UUID]:
    (tmp / "app").mkdir()
    (tmp / "app" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    repository = Repository(
        name=f"demo-trail-{uuid.uuid4().hex[:6]}",
        source_type=SourceType.ZIP,
        storage_path=str(tmp),
    )
    session.add(repository)
    session.commit()
    session.refresh(repository)

    audit = Audit(repository_id=repository.id, status=AuditStatus.QUEUED)
    session.add(audit)
    session.commit()
    session.refresh(audit)

    finding = Finding(
        audit_id=audit.id,
        category=Category.SECURITY,
        title="Export route reaches another tenant's report",
        severity=Severity.CRITICAL,
        confidence=Decimal("0.90"),
        origin=FindingOrigin.SCANNER,
        source_tool="semgrep",
        verification_status=VerificationStatus.SCANNER_CONFIRMED,
        verdict=Verdict.ISSUE,
        reasoning="the handler takes the report id from the request",
        fingerprint=uuid.uuid4().hex,
    )
    session.add(finding)
    session.flush()
    session.add(
        Evidence(
            finding_id=finding.id,
            file_path="app/main.py",
            line_start=1,
            line_end=1,
            snippet="print('hello')",
            kind=EvidenceKind.PRIMARY,
        )
    )
    session.commit()
    return audit.id, repository.id


def attempt(label: str, run) -> None:
    try:
        run()
    except SQLAlchemyError as exc:
        first = str(exc).strip().splitlines()[0][:150]
        print(f"  REFUSED  {label}\n           -> {first}")
    else:
        print(f"  ALLOWED  {label}   <-- this should never print")


def main() -> int:
    session = get_session_factory()()
    tmp = Path(tempfile.mkdtemp(prefix="demo-trail-"))
    audit_id, repository_id = seed(tmp, session)
    reviews_api.workflow_services = stub_services

    # Since step 3.6 the API needs a token. The demo acts as a reviewer account that
    # exists only for this run, rather than asking for a seeded password.
    from app.core.deps import get_current_user
    from app.core.seed import seed_users

    seed_users(
        {
            "SEED_ANALYST_EMAIL": f"demo-analyst-{uuid.uuid4().hex[:6]}@example.invalid",
            "SEED_ANALYST_PASSWORD": "demo-analyst-password",
            "SEED_REVIEWER_EMAIL": f"demo-reviewer-{uuid.uuid4().hex[:6]}@example.invalid",
            "SEED_REVIEWER_PASSWORD": "demo-reviewer-password",
            "SEED_ADMIN_EMAIL": f"demo-admin-{uuid.uuid4().hex[:6]}@example.invalid",
            "SEED_ADMIN_PASSWORD": "demo-admin-password",
        },
        session,
    )
    from app.db.models import User

    reviewer = session.execute(
        select(User).where(User.role == "reviewer")
    ).scalars().first()
    app.dependency_overrides[get_current_user] = lambda: reviewer

    with TestClient(app) as client:
        with postgres_checkpointer() as saver:
            start_or_resume(audit_id, services=stub_services(), checkpointer=saver)
        print(f"audit {audit_id}")
        print(f"paused at {pending_nodes(audit_id)} (awaiting a human)\n")

        review_id = session.execute(
            select(Review.id).where(Review.audit_id == audit_id)
        ).scalar_one()
        decision = client.post(
            f"/api/v1/reviews/{review_id}/decision",
            json={"decision": "approve"},
        )
        print(f"POST /api/v1/reviews/{review_id}/decision approve -> {decision.status_code}")
        body = decision.json()
        print(f"  review {body.get('review_status')}  audit {body.get('audit_status')}\n")

        print("Attempts to rewrite history")
        print("  through SQL:")
        attempt(
            "UPDATE audit_log SET actor = 'someone else'",
            lambda: (session.execute(text("UPDATE audit_log SET actor = 'someone else'")), session.commit()),
        )
        session.rollback()
        attempt(
            "DELETE FROM audit_log",
            lambda: (session.execute(text("DELETE FROM audit_log")), session.commit()),
        )
        session.rollback()
        attempt(
            "UPDATE review_decisions SET reason = 'rewritten'",
            lambda: (session.execute(text("UPDATE review_decisions SET reason = 'rewritten'")), session.commit()),
        )
        session.rollback()
        attempt(
            "DELETE FROM review_decisions",
            lambda: (session.execute(text("DELETE FROM review_decisions")), session.commit()),
        )
        session.rollback()

        print("  through the app's ORM:")
        trail_row = session.execute(
            select(AuditLog).where(AuditLog.object_id == str(audit_id)).limit(1)
        ).scalars().first()
        attempt(
            f"session.get(AuditLog).actor = 'someone else'  (row {str(trail_row.id)[:8]})",
            lambda: (setattr(trail_row, "actor", "someone else"), session.commit()),
        )
        session.rollback()
        attempt(
            "session.delete(audit_log row)",
            lambda: (session.delete(trail_row), session.commit()),
        )
        session.rollback()

        print("  as the application role due_diligence_app:")
        engine = get_engine()
        for statement in (
            "UPDATE audit_log SET actor = 'app role'",
            "UPDATE review_decisions SET reason = 'app role'",
        ):
            with engine.connect() as connection:
                try:
                    connection.execute(text('SET ROLE "due_diligence_app"'))
                    try:
                        connection.execute(text(statement))
                        print(f"  ALLOWED  {statement}   <-- this should never print")
                    except SQLAlchemyError as exc:
                        first = str(exc).strip().splitlines()[0][:150]
                        print(f"  REFUSED  {statement}\n           -> {first}")
                finally:
                    connection.rollback()
                    connection.execute(text("RESET ROLE"))
                    connection.commit()

        print("\nGET /api/v1/audits/{id}/audit-log")
        log = client.get(f"/api/v1/audits/{audit_id}/audit-log").json()
        print(f"{'when':<26} {'actor':<10} {'action':<26} {'object':<9} before -> after")
        print("-" * 130)
        for entry in log["entries"]:
            actor = entry["actor"]
            actor = "stub-user" if len(actor) == 36 else actor
            stamp = entry["created_at"][11:23]
            before = entry["before"] or {}
            after = entry["after"] or {}
            print(
                f"{stamp:<26} {actor:<10} {entry['action']:<26} "
                f"{entry['object_type']:<9} {before.get('stage') or before.get('status') or '-'}"
                f" -> {after.get('stage') or after.get('status') or '-'}"
            )
        print(f"\n{log['total']} rows, oldest first, none of them editable")

    session.execute(text("ALTER TABLE review_decisions DISABLE TRIGGER USER"))
    session.delete(session.get(Audit, audit_id))
    session.commit()
    session.execute(text("ALTER TABLE review_decisions ENABLE TRIGGER USER"))
    session.delete(session.get(Repository, repository_id))
    session.commit()
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
