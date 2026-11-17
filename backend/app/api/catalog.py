"""Read-only views the UI needs around the audit itself (step 3.10).

The audit, findings and report endpoints predate the dashboard: they answer "what is
this audit doing" and "what did it find", not "what has been run lately" or "what did
the risk score count". These routes add exactly that, and nothing that writes.

Everything here is computed from stored rows. The code viewer reads the read-only
snapshot the intake stored, never the running host filesystem, and every line it
returns has been through the same secret masking the scanners use - a finding detail
page must not become the one place a token can be read.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import get_current_user, require_roles
from app.db.enums import UserRole
from app.db.models import (
    Audit,
    Evidence,
    Finding,
    FindingEvent,
    Repository,
    RiskFactor,
    User,
)
from app.db.session import get_session
from app.risk.assessment import current_assessment
from app.scanners.common import redact_text

router = APIRouter(prefix="/api/v1", tags=["catalog"])

REPO_ROOT = Path(__file__).resolve().parents[3]


# --- identity ----------------------------------------------------------------


class MeOut(BaseModel):
    id: str
    email: str
    role: str


@router.get("/auth/me", response_model=MeOut, summary="The signed-in account")
def me(user: User = Depends(get_current_user)) -> MeOut:
    return MeOut(id=str(user.id), email=user.email, role=user.role.value)


# --- dashboard and repositories ----------------------------------------------


class AuditRow(BaseModel):
    id: str
    repository_id: str
    repository_name: str
    status: str
    current_stage: str | None
    risk_level: str | None
    cost_usd: float
    budget_usd: float | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class AuditRows(BaseModel):
    audits: list[AuditRow]
    total: int


def _audit_row(session: Session, audit: Audit, repository: Repository | None) -> AuditRow:
    assessment = current_assessment(session, audit.id)
    return AuditRow(
        id=str(audit.id),
        repository_id=str(audit.repository_id),
        repository_name=repository.name if repository else "(deleted repository)",
        status=audit.status.value,
        current_stage=audit.current_stage.value if audit.current_stage else None,
        risk_level=assessment.overall_level.value if assessment else None,
        cost_usd=float(audit.cost_usd or 0),
        budget_usd=float(audit.budget_usd) if audit.budget_usd is not None else None,
        created_at=audit.created_at,
        started_at=audit.started_at,
        finished_at=audit.finished_at,
    )


@router.get("/audits", response_model=AuditRows, summary="Recent audits, newest first")
def list_audits(
    status_filter: str | None = Query(None, alias="status", description="audit status"),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(require_roles(UserRole.ANALYST, UserRole.REVIEWER)),
    session: Session = Depends(get_session),
) -> AuditRows:
    stmt = select(Audit).order_by(Audit.created_at.desc()).limit(limit)
    if status_filter:
        stmt = stmt.where(Audit.status == status_filter)
    audits = list(session.execute(stmt).scalars())
    repositories = {
        row.id: row
        for row in session.execute(
            select(Repository).where(
                Repository.id.in_({audit.repository_id for audit in audits})
            )
        ).scalars()
    }
    return AuditRows(
        audits=[_audit_row(session, audit, repositories.get(audit.repository_id)) for audit in audits],
        total=len(audits),
    )


class LastAudit(BaseModel):
    id: str
    status: str
    risk_level: str | None
    finished_at: datetime | None


class RepositoryRow(BaseModel):
    id: str
    name: str
    source_type: str
    source_ref: str | None
    size_bytes: int | None
    created_at: datetime
    audit_count: int
    last_audit: LastAudit | None


class RepositoryRows(BaseModel):
    repositories: list[RepositoryRow]
    total: int


@router.get("/repositories", response_model=RepositoryRows, summary="Repositories and their latest audit")
def list_repositories(
    user: User = Depends(require_roles(UserRole.ANALYST, UserRole.REVIEWER)),
    session: Session = Depends(get_session),
) -> RepositoryRows:
    repositories = list(
        session.execute(select(Repository).order_by(Repository.created_at.desc())).scalars()
    )
    audits = list(
        session.execute(select(Audit).order_by(Audit.created_at.desc())).scalars()
    )
    by_repo: dict[uuid.UUID, list[Audit]] = {}
    for audit in audits:
        by_repo.setdefault(audit.repository_id, []).append(audit)
    rows = []
    for repository in repositories:
        owned = by_repo.get(repository.id, [])
        latest = owned[0] if owned else None
        assessment = current_assessment(session, latest.id) if latest else None
        rows.append(
            RepositoryRow(
                id=str(repository.id),
                name=repository.name,
                source_type=repository.source_type.value,
                source_ref=repository.source_ref,
                size_bytes=repository.size_bytes,
                created_at=repository.created_at,
                audit_count=len(owned),
                last_audit=(
                    LastAudit(
                        id=str(latest.id),
                        status=latest.status.value,
                        risk_level=assessment.overall_level.value if assessment else None,
                        finished_at=latest.finished_at,
                    )
                    if latest
                    else None
                ),
            )
        )
    return RepositoryRows(repositories=rows, total=len(rows))


# --- risk breakdown -----------------------------------------------------------


class FactorRow(BaseModel):
    finding_id: str
    title: str
    severity: str
    severity_weight: float
    evidence_confidence: float
    exposure_multiplier: float
    finding_score: float
    notes: str | None
    hints: list[str]


class RiskOut(BaseModel):
    audit_id: str
    level: str
    floor: str | None
    score: float | None
    rule: str | None
    rationale: str | None
    status: str
    supersedes_id: str | None
    counts: dict[str, int]
    rejected: int
    escalations: list[str]
    factors: list[FactorRow]
    business_risk_note: str | None


@router.get("/audits/{audit_id}/risk", response_model=RiskOut, summary="Risk assessment and its factors")
def read_risk(
    audit_id: uuid.UUID,
    user: User = Depends(require_roles(UserRole.ANALYST, UserRole.REVIEWER, UserRole.ADMIN)),
    session: Session = Depends(get_session),
) -> RiskOut:
    audit = session.get(Audit, audit_id)
    if audit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="audit not found")
    assessment = current_assessment(session, audit_id)
    if assessment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="this audit has no risk assessment yet",
        )

    summary = assessment.technical_summary or {}
    hints_by_finding = {
        str(item.get("finding_id")): [str(hint) for hint in item.get("hints") or []]
        for item in summary.get("factors") or []
        if isinstance(item, dict)
    }
    factors = []
    for factor in session.execute(
        select(RiskFactor)
        .where(RiskFactor.assessment_id == assessment.id)
        .order_by(RiskFactor.finding_score.desc())
    ).scalars():
        finding = session.get(Finding, factor.finding_id)
        factors.append(
            FactorRow(
                finding_id=str(factor.finding_id),
                title=finding.title if finding else "(finding deleted)",
                severity=finding.severity.value if finding else "info",
                severity_weight=float(factor.severity_weight),
                evidence_confidence=float(factor.evidence_confidence),
                exposure_multiplier=float(factor.exposure_multiplier),
                finding_score=float(factor.finding_score),
                notes=factor.notes,
                hints=hints_by_finding.get(str(factor.finding_id), []),
            )
        )
    counts = summary.get("counts") or {}
    return RiskOut(
        audit_id=str(audit_id),
        level=assessment.overall_level.value,
        floor=summary.get("highest"),
        score=float(assessment.overall_score) if assessment.overall_score is not None else None,
        rule=assessment.rule_applied,
        rationale=assessment.rationale,
        status=assessment.status.value,
        supersedes_id=str(assessment.supersedes_id) if assessment.supersedes_id else None,
        counts={key: int(value) for key, value in counts.items()},
        rejected=int(summary.get("rejected") or 0),
        escalations=[str(name) for name in summary.get("escalations") or []],
        factors=factors,
        business_risk_note=assessment.business_risk_note,
    )


# --- code viewer --------------------------------------------------------------


class CodeLine(BaseModel):
    number: int
    text: str
    masked: bool
    highlighted: bool


class CodeOut(BaseModel):
    finding_id: str
    file_path: str
    line_start: int
    line_end: int
    highlight_start: int
    highlight_end: int
    lines: list[CodeLine]
    truncated: bool
    masked: bool


MAX_CODE_BYTES = 400_000


@router.get(
    "/findings/{finding_id}/code",
    response_model=CodeOut,
    summary="The stored snapshot around a finding, masked and read-only",
)
def read_code(
    finding_id: uuid.UUID,
    context: int = Query(8, ge=0, le=50),
    user: User = Depends(require_roles(UserRole.ANALYST, UserRole.REVIEWER)),
    session: Session = Depends(get_session),
) -> CodeOut:
    finding = session.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="finding not found")
    evidence = session.execute(
        select(Evidence)
        .where(Evidence.finding_id == finding_id)
        .order_by(Evidence.file_path, Evidence.line_start)
        .limit(1)
    ).scalars().first()
    if evidence is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="this finding has no evidence location"
        )
    audit = session.get(Audit, finding.audit_id)
    repository = session.get(Repository, audit.repository_id) if audit else None
    if repository is None or not repository.storage_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="the repository snapshot is no longer available"
        )

    root = Path(repository.storage_path).resolve()
    target = (root / evidence.file_path).resolve()
    if target != root and root not in target.parents:
        # A stored path that escapes the snapshot is a bug or an attack, never a read.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid evidence path")
    if not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="the file is not in the stored snapshot"
        )

    raw = target.read_bytes()[:MAX_CODE_BYTES]
    truncated = target.stat().st_size > MAX_CODE_BYTES
    text = raw.decode("utf-8", errors="replace")
    file_lines = text.splitlines()
    first = max(1, evidence.line_start - context)
    last = min(len(file_lines), evidence.line_end + context)
    lines: list[CodeLine] = []
    any_masked = False
    for number in range(first, last + 1):
        original = file_lines[number - 1]
        masked_line = redact_text(original)
        was_masked = masked_line != original
        any_masked = any_masked or was_masked
        lines.append(
            CodeLine(
                number=number,
                text=masked_line,
                masked=was_masked,
                highlighted=evidence.line_start <= number <= evidence.line_end,
            )
        )

    return CodeOut(
        finding_id=str(finding_id),
        file_path=evidence.file_path,
        line_start=first,
        line_end=last,
        highlight_start=evidence.line_start,
        highlight_end=evidence.line_end,
        lines=lines,
        truncated=truncated,
        masked=any_masked,
    )


class FindingHistoryRow(BaseModel):
    id: str
    from_status: str | None
    to_status: str
    actor: str
    note: str | None
    created_at: datetime


class FindingHistoryOut(BaseModel):
    finding_id: str
    events: list[FindingHistoryRow]


@router.get(
    "/findings/{finding_id}/history",
    response_model=FindingHistoryOut,
    summary="Every recorded status change for a finding",
)
def read_history(
    finding_id: uuid.UUID,
    user: User = Depends(require_roles(UserRole.ANALYST, UserRole.REVIEWER)),
    session: Session = Depends(get_session),
) -> FindingHistoryOut:
    if session.get(Finding, finding_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="finding not found")
    rows = session.execute(
        select(FindingEvent)
        .where(FindingEvent.finding_id == finding_id)
        .order_by(FindingEvent.created_at.asc())
    ).scalars()
    return FindingHistoryOut(
        finding_id=str(finding_id),
        events=[
            FindingHistoryRow(
                id=str(row.id),
                from_status=row.from_status.value if row.from_status else None,
                to_status=row.to_status.value,
                actor=row.actor,
                note=row.note,
                created_at=row.created_at,
            )
            for row in rows
        ],
    )


# --- admin: users, settings, benchmark ---------------------------------------


class UserRow(BaseModel):
    id: str
    email: str
    role: str
    is_active: bool
    created_at: datetime


class UserRows(BaseModel):
    users: list[UserRow]
    total: int


@router.get("/users", response_model=UserRows, summary="Accounts and roles (admin)")
def list_users(
    user: User = Depends(require_roles(UserRole.ADMIN)),
    session: Session = Depends(get_session),
) -> UserRows:
    users = list(session.execute(select(User).order_by(User.email)).scalars())
    return UserRows(
        users=[
            UserRow(
                id=str(row.id),
                email=row.email,
                role=row.role.value,
                is_active=row.is_active,
                created_at=row.created_at,
            )
            for row in users
        ],
        total=len(users),
    )


class SettingsOut(BaseModel):
    """Deployment configuration, read-only. No secret is ever included."""

    llm_default_model: str
    llm_triage_model: str
    llm_fallback_model: str
    llm_fallback_enabled: bool
    llm_temperature: float
    llm_max_output_tokens: int
    max_upload_bytes: int
    default_budget_usd: float | None
    retention_days: int | None
    notes: list[str]


@router.get("/settings", response_model=SettingsOut, summary="Read-only deployment settings (admin)")
def read_settings(user: User = Depends(require_roles(UserRole.ADMIN))) -> SettingsOut:
    return SettingsOut(
        llm_default_model=settings.llm_default_model,
        llm_triage_model=settings.llm_triage_model,
        llm_fallback_model=settings.llm_fallback_model,
        llm_fallback_enabled=settings.llm_fallback_enabled,
        llm_temperature=settings.llm_temperature,
        llm_max_output_tokens=settings.llm_max_output_tokens,
        max_upload_bytes=settings.max_upload_bytes,
        default_budget_usd=None,
        retention_days=None,
        notes=[
            "Model routing and limits come from the deployment environment; changing them "
            "restarts the API rather than editing rows here.",
            "A per-audit budget is stored on the audit itself (audits.budget_usd); when it "
            "is unset the run has no cap.",
            "Retention is not enforced by the application yet; backups are kept according "
            "to the deployment's own policy.",
        ],
    )


class BenchmarkRun(BaseModel):
    configuration: str
    precision: float | None
    recall: float | None
    false_positives: int | None
    evidence_accuracy: float | None
    severity_accuracy: float | None
    cost_usd: float | None
    runtime_seconds: float | None
    reproducibility: float | None
    cross_file_recall: float | None
    runs: int | None


class BenchmarkOut(BaseModel):
    runs: list[BenchmarkRun]
    generated_at: datetime | None
    note: str | None


@router.get("/benchmarks", response_model=BenchmarkOut, summary="Benchmark runs by configuration (admin)")
def read_benchmarks(user: User = Depends(require_roles(UserRole.ADMIN))) -> BenchmarkOut:
    """The comparison the four-way experiment writes; empty until step 3.11 runs it."""
    path = REPO_ROOT / "benchmark" / "results.json"
    if not path.is_file():
        return BenchmarkOut(
            runs=[],
            generated_at=None,
            note="No benchmark runs have been recorded yet. Step 3.11 writes "
            "benchmark/results.json and this table fills in.",
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("runs", payload) if isinstance(payload, dict) else payload
    allowed = set(BenchmarkRun.model_fields)
    runs = [
        BenchmarkRun(**{key: value for key, value in row.items() if key in allowed})
        for row in rows
        if isinstance(row, dict) and "configuration" in row
    ]
    generated = payload.get("generated_at") if isinstance(payload, dict) else None
    return BenchmarkOut(
        runs=runs,
        generated_at=datetime.fromisoformat(generated) if generated else None,
        note=payload.get("note") if isinstance(payload, dict) else None,
    )


__all__ = ["router"]
