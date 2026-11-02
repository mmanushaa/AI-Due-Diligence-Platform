import uuid
import html as _html
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, desc, select
from sqlalchemy.orm import Session, selectinload

from app.api.audits import _to_out
from app.db.enums import AuditStatus
from app.db.models import Audit, Evidence, Finding, Repository
from app.db.session import get_session
from app.schemas.findings import (
    AuditOut,
    EvidenceOut,
    FindingDetail,
    FindingEntry,
    FindingList,
    FindingSummary,
    RepositoryOut,
)
from app.core.deps import get_current_user as get_current_user_dep

router = APIRouter(prefix="/api/v1", tags=["findings"])

SEVERITY_ORDER = (
    "critical",
    "high",
    "medium",
    "low",
    "info",
)


def _highest_severity(grouped: dict[str, dict[str, list[Finding]]]) -> str:
    for sev in SEVERITY_ORDER:
        if any(sev in grouped[category] for category in grouped):
            return sev
    return "none"


@router.get(
    "/audits/{audit_id:uuid}/findings",
    response_model=FindingList,
    summary="List findings for an audit with optional filters",
)
def list_findings(
    audit_id: uuid.UUID,
    category: str | None = Query(None, description="Category to filter by"),
    severity: str | None = Query(None, description="Severity to filter by"),
    status: str | None = Query(None, description="Verification status to filter by"),
    file: str | None = Query(None, description="file path substring filter"),
    user=Depends(get_current_user_dep),
    session: Session = Depends(get_session),
):
    audit = session.get(Audit, audit_id)
    if not audit:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="audit not found")

    stmt = (
        select(Finding)
        .where(Finding.audit_id == audit_id)
        .options(selectinload(Finding.evidence))
        .order_by(desc(Finding.created_at))
    )
    if category:
        stmt = stmt.where(Finding.category == category)
    if severity:
        stmt = stmt.where(Finding.severity == severity)
    if status:
        stmt = stmt.where(Finding.verification_status == status)
    if file:
        stmt = stmt.where(Finding.file_path.icontains(file))

    findings = session.execute(stmt).scalars().unique().all()

    grouped: dict[str, dict[str, list[Finding]]] = defaultdict(lambda: defaultdict(list))
    for finding in findings:
        grouped[finding.category.value][finding.severity.value].append(finding)

    by_category: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    high_critical = 0
    for finding in findings:
        by_category[finding.category.value] = by_category.get(finding.category.value, 0) + 1
        by_severity[finding.severity.value] = by_severity.get(finding.severity.value, 0) + 1
        if finding.severity.value in ("high", "critical"):
            high_critical += 1

    return FindingList(
        audit=AuditOut(
            id=str(audit.id),
            status=audit.status.value,
            current_stage=audit.current_stage.value if audit.current_stage else None,
            started_at=audit.started_at,
            finished_at=audit.finished_at,
            error=audit.error,
        ),
        repository=RepositoryOut(
            id=str(audit.repository_id)
            if audit.repository_id
            else "",
            name="",
            source_type="",
            source_ref=None,
            storage_path="",
            sha256=None,
            size_bytes=None,
            created_at=None,
        )
        if False
        else None,
        summary=FindingSummary(
            total=len(findings),
            by_category=by_category,
            by_severity=by_severity,
            high_critical=high_critical,
        ),
        findings=[
            FindingEntry(
                id=str(f.id),
                title=f.title,
                category=f.category.value,
                severity=f.severity.value,
                status=f.verification_status.value,
                file_path=f.file_path,
                line_start=f.line_start,
                line_end=f.line_end,
                source_tool=f.source_tool.value if f.source_tool else None,
                fingerprint=f.fingerprint,
            )
            for f in sorted(
                findings,
                key=lambda f: (
                    -SEVERITY_ORDER.index(f.severity.value)
                    if f.severity.value in SEVERITY_ORDER
                    else 0,
                    f.created_at.isoformat(),
                ),
            )
        ],
        grouped={
            category: {sev: [str(f.id) for f in fxs] for sev, fxs in sevmap.items()}
            for category, sevmap in grouped.items()
        },
    )


@router.get(
    "/findings/{finding_id:uuid}",
    response_model=FindingDetail,
    summary="Get a single finding with evidence",
)
def get_finding(
    finding_id: uuid.UUID,
    user=Depends(get_current_user_dep),
    session: Session = Depends(get_session),
):
    stmt = (
        select(Finding)
        .where(Finding.id == finding_id)
        .options(selectinload(Finding.evidence))
    )
    finding = session.execute(stmt).scalars().first()
    if not finding:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="finding not found")

    audit = session.get(Audit, finding.audit_id)
    repository_id = getattr(audit, "repository_id", None)

    return FindingDetail(
        id=str(finding.id),
        title=finding.title,
        description=finding.description,
        category=finding.category.value,
        severity=finding.severity.value,
        status=finding.verification_status.value,
        file_path=finding.file_path,
        line_start=finding.line_start,
        line_end=finding.line_end,
        source_tool=finding.source_tool.value if finding.source_tool else None,
        scanner_tool=getattr(finding, "scanner_tool", None),
        fingerprint=finding.fingerprint,
        recommendation=getattr(finding, "recommendation", None),
        verdict=getattr(finding, "verdict", None),
        evidence=[
            EvidenceOut(
                id=str(ev.id),
                kind=ev.kind.value,
                file_path=ev.file_path,
                line_start=ev.line_start,
                line_end=ev.line_end,
                snippet=ev.snippet,
                label=ev.label,
            )
            for ev in (finding.evidence or [])
        ],
        audit=AuditOut(
            id=str(audit.id) if audit else "",
            status=audit.status.value if audit else "",
            current_stage=audit.current_stage.value if audit and audit.current_stage else None,
            started_at=audit.started_at if audit else None,
            finished_at=audit.finished_at if audit else None,
            error=audit.error if audit else None,
        )
        if audit
        else None,
        repository_id=str(repository_id) if repository_id else None,
    )


@router.get(
    "/audits/{audit_id:uuid}/report",
    summary="Basic HTML report for an audit",
    response_class=Response,
)
def audit_report(
    audit_id: uuid.UUID,
    user=Depends(get_current_user_dep),
    session: Session = Depends(get_session),
):
    audit = session.get(Audit, audit_id)
    if not audit:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="audit not found")

    repository = session.get(Repository, audit.repository_id) if audit.repository_id else None

    stmt = (
        select(Finding)
        .where(Finding.audit_id == audit_id)
        .options(selectinload(Finding.evidence))
        .order_by(desc(Finding.created_at))
    )
    findings = session.execute(stmt).scalars().unique().all()

    grouped: dict[str, dict[str, list[Finding]]] = defaultdict(lambda: defaultdict(list))
    for finding in findings:
        grouped[finding.category.value][finding.severity.value].append(finding)

    rows_html = ""
    for category in sorted(grouped):
        for severity in sorted(grouped[category]):
            for finding in grouped[category][severity]:
                evidence_rows = ""
                for ev in (finding.evidence or []):
                    snippet = _html.escape(ev.snippet or "") if ev.snippet else ""
                    evidence_rows += (
                        "              <tr>\n"
                        f"                <td>{_html.escape(ev.file_path)}</td>\n"
                        f"                <td>{ev.line_start}-{ev.line_end}</td>\n"
                        f"                <td>{_html.escape(ev.kind.value)}</td>\n"
                        f"                <td>{snippet}</td>\n"
                        "              </tr>\n"
                    )
                rows_html += (
                    "          <div class=\"finding\">\n"
                    f"            <div class=\"finding-title\">{_html.escape(finding.title)}</div>\n"
                    "            <table class=\"evidence\">\n"
                    "              <thead>\n"
                    "                <tr>\n"
                    "                  <th>File</th>\n"
                    "                  <th>Lines</th>\n"
                    "                  <th>Kind</th>\n"
                    "                  <th>Snippet</th>\n"
                    "                </tr>\n"
                    "              </thead>\n"
                    "              <tbody>\n"
                    f"{evidence_rows}"
                    "              </tbody>\n"
                    "            </table>\n"
                    "          </div>\n"
                )

    now = datetime.now(timezone.utc).isoformat()
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Audit Report — {_html.escape(str(audit_id))}</title>
  <style>
    body {{
      font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      color: #1f2937;
      background: #f9fafb;
      margin: 0;
      padding: 2rem;
    }}
    h1 {{
      font-size: 1.5rem;
      margin: 0 0 0.25rem;
    }}
    .meta {{
      color: #6b7280;
      margin-bottom: 1.5rem;
    }}
    .summary {{
      display: flex;
      gap: 1rem;
      margin-bottom: 2rem;
    }}
    .card {{
      border: 1px solid #e5e7eb;
      border-radius: 8px;
      padding: 1rem;
      background: #fff;
      min-width: 140px;
    }}
    .card .label {{
      font-size: 0.75rem;
      color: #6b7280;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }}
    .card .value {{
      font-size: 1.5rem;
      font-weight: 600;
      margin-top: 0.25rem;
    }}
    .section {{
      margin-top: 2rem;
    }}
    .section h2 {{
      font-size: 1rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #6b7280;
      margin: 0 0 0.5rem;
      border-bottom: 1px solid #e5e7eb;
      padding-bottom: 0.25rem;
    }}
    .finding {{
      margin-bottom: 1rem;
      padding: 1rem;
      background: #fff;
      border: 1px solid #e5e7eb;
      border-radius: 8px;
    }}
    .finding-title {{
      font-weight: 600;
      margin-bottom: 0.75rem;
    }}
    .badge {{
      display: inline-block;
      padding: 0.15rem 0.5rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 600;
      margin-left: 0.5rem;
      color: #fff;
    }}
    .badge-critical {{ background: #b91c1c; }}
    .badge-high {{ background: #c2410c; }}
    .badge-medium {{ background: #b45309; }}
    .badge-low {{ background: #a5a5a5; }}
    .badge-info {{ background: #3b82f6; }}
    table.evidence {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
    }}
    table.evidence th,
    table.evidence td {{
      text-align: left;
      border: 1px solid #e5e7eb;
      padding: 0.35rem 0.5rem;
    }}
    table.evidence th {{
      background: #f3f4f6;
      color: #6b7280;
    }}
    table.evidence td {{
      max-width: 30rem;
      word-break: break-word;
    }}
  </style>
</head>
<body>
  <h1>Audit Report</h1>
  <div class="meta">
    Audit {_html.escape(str(audit_id))} &middot;
    {_html.escape(repository.name if repository else "unknown")} &middot;
    {_html.escape(audit.status.value)} &middot;
    {_html.escape(now)}
  </div>
  <div class="summary">
    <div class="card">
      <div class="label">Findings</div>
      <div class="value">{len(findings)}</div>
    </div>
    <div class="card">
      <div class="label">Categories</div>
      <div class="value">{len(grouped)}</div>
    </div>
    <div class="card">
      <div class="label">Highest severity</div>
      <div class="value">{_highest_severity(grouped)}</div>
    </div>
  </div>
  <div class="section">
    <h2>Findings by category and severity</h2>
{rows_html}
  </div>
</body>
</html>
"""
