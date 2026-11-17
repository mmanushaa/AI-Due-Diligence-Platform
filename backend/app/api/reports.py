"""Report endpoints (step 3.9): the executive HTML report and its PDF export."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.core.audit import record
from app.core.deps import require_roles
from app.db.enums import UserRole
from app.db.models import Audit, User
from app.db.session import get_session
from app.reports.builder import build_report
from app.reports.render import PdfUnavailable, render_html, render_pdf

router = APIRouter(prefix="/api/v1", tags=["reports"])


def _document(session: Session, audit_id: uuid.UUID, user, fmt: str):
    audit = session.get(Audit, audit_id)
    if audit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="audit not found")
    try:
        document = build_report(session, audit_id)
    except LookupError as exc:  # pragma: no cover - guarded by the lookup above
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="audit not found"
        ) from exc

    record(
        session,
        actor=user.id,
        action="report_generated",
        object_type="audit",
        object_id=audit.id,
        after={
            "format": fmt,
            "findings": sum(document.totals.values()) if document.totals else 0,
            "claims": len(document.claims),
            "dropped_claims": len(document.citations_dropped),
        },
    )
    session.commit()
    return document


@router.get(
    "/audits/{audit_id}/report",
    response_class=HTMLResponse,
    summary="Executive report (HTML)",
)
def audit_report(
    audit_id: uuid.UUID,
    user: User = Depends(require_roles(UserRole.ANALYST, UserRole.REVIEWER)),
    session: Session = Depends(get_session),
) -> HTMLResponse:
    document = _document(session, audit_id, user, "html")
    return HTMLResponse(content=render_html(document))


@router.get(
    "/audits/{audit_id}/report.pdf",
    response_class=Response,
    summary="Executive report (PDF)",
)
def audit_report_pdf(
    audit_id: uuid.UUID,
    user: User = Depends(require_roles(UserRole.ANALYST, UserRole.REVIEWER)),
    session: Session = Depends(get_session),
) -> Response:
    document = _document(session, audit_id, user, "pdf")
    try:
        pdf = render_pdf(render_html(document))
    except PdfUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    filename = f"audit-{audit_id}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )
