"""Read-only Pydantic models for findings, evidence and audit snapshots."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class EvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    file_path: str
    line_start: int
    line_end: int
    snippet: str | None
    kind: str
    label: str | None = None


class FindingSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total: int = 0
    by_category: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    high_critical: int = 0


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    current_stage: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class RepositoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    source_type: str
    source_ref: str | None = None
    storage_path: str
    sha256: str | None = None
    size_bytes: int | None = None
    created_at: datetime


class FindingEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    category: str
    severity: str
    status: str
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    source_tool: str | None = None
    fingerprint: str


class FindingList(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    audit: AuditOut
    repository: RepositoryOut | None = None
    summary: FindingSummary
    findings: list[FindingEntry] = []
    grouped: dict[str, dict[str, list[str]]] = {}


class FindingDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    description: str | None = None
    category: str
    severity: str
    status: str
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    source_tool: str | None = None
    scanner_tool: str | None = None
    fingerprint: str
    recommendation: str | None = None
    verdict: str | None = None
    evidence: list[EvidenceOut] = []
    audit: AuditOut | None = None
    repository_id: str | None = None


def _to_out(model) -> dict:
    """Lightweight serialisation used by the findings endpoints."""
    if model is None:
        return None
    return {
        "id": str(getattr(model, "id", None)),
        "name": getattr(model, "name", None),
        "status": getattr(model, "status", None),
        "created_at": getattr(model, "created_at", None),
        "updated_at": getattr(model, "updated_at", None),
    }
