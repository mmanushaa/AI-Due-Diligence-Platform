"""SQLAlchemy 2 models mirroring docs/5. Backend Schema.md.

Every table carries `id uuid pk default gen_random_uuid()` and
`created_at timestamptz default now()` unless the schema tab says otherwise.
Foreign keys owned by an audit use ON DELETE CASCADE; optional references to
users or to a superseded row use ON DELETE SET NULL.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, IdMixin
from app.db.enums import (
    AgentName,
    AssessmentStatus,
    AuditStatus,
    Category,
    EvalConfig,
    EvidenceKind,
    FindingOrigin,
    ReviewDecision,
    ReviewStatus,
    ScannerRunStatus,
    ScannerTool,
    Severity,
    SourceType,
    Stage,
    SymbolKind,
    UserRole,
    VerificationStatus,
    Verdict,
    pg_enum,
)

# Set by the embedding model. Changing it needs a new migration.
EMBEDDING_DIM = 1536


class User(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[UserRole] = mapped_column(pg_enum(UserRole, "user_role"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class Repository(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "repositories"

    name: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[SourceType] = mapped_column(
        pg_enum(SourceType, "source_type"), nullable=False
    )
    source_ref: Mapped[str | None] = mapped_column(Text)  # filename or URL
    storage_path: Mapped[str | None] = mapped_column(Text)  # read-only snapshot
    sha256: Mapped[str | None] = mapped_column(Text)  # archive hash
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Audit(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "audits"

    repository_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[AuditStatus] = mapped_column(
        pg_enum(AuditStatus, "audit_status"), nullable=False
    )
    current_stage: Mapped[Stage | None] = mapped_column(pg_enum(Stage, "stage"))
    config: Mapped[dict | None] = mapped_column(JSONB)  # models, budget, scanner versions
    workflow_thread_id: Mapped[str | None] = mapped_column(Text)  # LangGraph thread
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, server_default=text("0")
    )
    budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class File(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "files"
    __table_args__ = (Index("ix_files_audit_id_path", "audit_id", "path"),)

    audit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("audits.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(Text)
    is_excluded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    exclude_reason: Mapped[str | None] = mapped_column(Text)


class Symbol(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "symbols"
    __table_args__ = (
        Index(
            "ix_symbols_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )

    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), nullable=False
    )
    audit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("audits.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[SymbolKind] = mapped_column(pg_enum(SymbolKind, "symbol_kind"), nullable=False)
    line_start: Mapped[int] = mapped_column(Integer, nullable=False)
    line_end: Mapped[int] = mapped_column(Integer, nullable=False)
    signature: Mapped[str | None] = mapped_column(Text)
    # Column is named `metadata`, which is reserved on the declarative class.
    symbol_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB)


class Chunk(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunks_content_tsv", "content_tsv", postgresql_using="gin"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), nullable=False
    )
    audit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("audits.id", ondelete="CASCADE"), nullable=False
    )
    symbol_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("symbols.id", ondelete="SET NULL")
    )
    line_start: Mapped[int] = mapped_column(Integer, nullable=False)
    line_end: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_tsv: Mapped[str | None] = mapped_column(TSVECTOR)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    token_count: Mapped[int | None] = mapped_column(Integer)


class CallEdge(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "call_edges"
    __table_args__ = (
        Index("ix_call_edges_caller_symbol_id", "caller_symbol_id"),
        Index("ix_call_edges_callee_symbol_id", "callee_symbol_id"),
    )

    audit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("audits.id", ondelete="CASCADE"), nullable=False
    )
    caller_symbol_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False
    )
    callee_symbol_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("symbols.id", ondelete="SET NULL")
    )
    callee_name: Mapped[str] = mapped_column(Text, nullable=False)


class ModuleEdge(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "module_edges"
    __table_args__ = (Index("ix_module_edges_from_file_id", "from_file_id"),)

    audit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("audits.id", ondelete="CASCADE"), nullable=False
    )
    from_file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), nullable=False
    )
    to_file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)


class ScannerRun(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "scanner_runs"

    audit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("audits.id", ondelete="CASCADE"), nullable=False
    )
    tool: Mapped[ScannerTool] = mapped_column(pg_enum(ScannerTool, "scanner_tool"), nullable=False)
    version: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ScannerRunStatus] = mapped_column(
        pg_enum(ScannerRunStatus, "scanner_run_status"), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    raw_output_path: Mapped[str | None] = mapped_column(Text)


class Finding(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "findings"
    __table_args__ = (
        UniqueConstraint("audit_id", "fingerprint", name="uq_findings_audit_fingerprint"),
        Index("ix_findings_audit_id_severity", "audit_id", "severity"),
        Index(
            "ix_findings_audit_id_verification_status", "audit_id", "verification_status"
        ),
    )

    audit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("audits.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[Category] = mapped_column(pg_enum(Category, "category"), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[Severity] = mapped_column(pg_enum(Severity, "severity"), nullable=False)
    # 0 to 1; not a calibrated probability.
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    origin: Mapped[FindingOrigin] = mapped_column(
        pg_enum(FindingOrigin, "finding_origin"), nullable=False
    )
    source_tool: Mapped[str | None] = mapped_column(Text)  # semgrep rule id, agent name
    verification_status: Mapped[VerificationStatus] = mapped_column(
        pg_enum(VerificationStatus, "verification_status"), nullable=False
    )
    verdict: Mapped[Verdict | None] = mapped_column(pg_enum(Verdict, "verdict"))
    reasoning: Mapped[str | None] = mapped_column(Text)
    business_impact: Mapped[str | None] = mapped_column(Text)
    recommendation: Mapped[str | None] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)  # dedupe key, per audit


class Evidence(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "evidence"
    __table_args__ = (Index("ix_evidence_finding_id", "finding_id"),)

    finding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("findings.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("files.id", ondelete="SET NULL")
    )
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    line_start: Mapped[int] = mapped_column(Integer, nullable=False)
    line_end: Mapped[int] = mapped_column(Integer, nullable=False)
    snippet: Mapped[str | None] = mapped_column(Text)  # secrets masked
    kind: Mapped[EvidenceKind] = mapped_column(
        pg_enum(EvidenceKind, "evidence_kind"), nullable=False
    )
    retrieval_score: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))


class FindingEvent(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "finding_events"

    finding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("findings.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[VerificationStatus | None] = mapped_column(
        pg_enum(VerificationStatus, "verification_status")
    )
    to_status: Mapped[VerificationStatus] = mapped_column(
        pg_enum(VerificationStatus, "verification_status"), nullable=False
    )
    actor: Mapped[str] = mapped_column(Text, nullable=False)  # system, agent name, user id
    note: Mapped[str | None] = mapped_column(Text)


class RiskAssessment(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "risk_assessments"

    audit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("audits.id", ondelete="CASCADE"), nullable=False
    )
    overall_level: Mapped[Severity] = mapped_column(
        pg_enum(Severity, "severity"), nullable=False
    )
    overall_score: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    rule_applied: Mapped[str | None] = mapped_column(Text)  # e.g. severity_floor
    rationale: Mapped[str | None] = mapped_column(Text)
    technical_summary: Mapped[dict | None] = mapped_column(JSONB)
    business_risk_note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[AssessmentStatus] = mapped_column(
        pg_enum(AssessmentStatus, "assessment_status"), nullable=False
    )
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("risk_assessments.id", ondelete="SET NULL")
    )


class RiskFactor(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "risk_factors"

    assessment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("risk_assessments.id", ondelete="CASCADE"), nullable=False
    )
    finding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("findings.id", ondelete="CASCADE"), nullable=False
    )
    severity_weight: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    evidence_confidence: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    exposure_multiplier: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    finding_score: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)


class Review(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "reviews"

    audit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("audits.id", ondelete="CASCADE"), nullable=False
    )
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("risk_assessments.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[ReviewStatus] = mapped_column(
        pg_enum(ReviewStatus, "review_status"), nullable=False
    )
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewDecisionRow(IdMixin, CreatedAtMixin, Base):
    """Append-only: no update or delete triggers yet (step 3.5)."""

    __tablename__ = "review_decisions"

    review_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reviews.id", ondelete="CASCADE"), nullable=False
    )
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    decision: Mapped[ReviewDecision] = mapped_column(
        pg_enum(ReviewDecision, "review_decision"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text)  # required unless approve
    original_level: Mapped[Severity | None] = mapped_column(pg_enum(Severity, "severity"))
    final_level: Mapped[Severity | None] = mapped_column(pg_enum(Severity, "severity"))


class Report(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "reports"

    audit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("audits.id", ondelete="CASCADE"), nullable=False
    )
    html_content: Mapped[str | None] = mapped_column("html", Text)
    pdf_path: Mapped[str | None] = mapped_column(Text)
    generated_by: Mapped[str | None] = mapped_column(Text)
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("risk_assessments.id", ondelete="SET NULL")
    )


class AuditLog(IdMixin, CreatedAtMixin, Base):
    """Append-only: no update or delete triggers yet (step 3.5)."""

    __tablename__ = "audit_log"

    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    object_type: Mapped[str] = mapped_column(Text, nullable=False)
    object_id: Mapped[str] = mapped_column(Text, nullable=False)
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)


class AgentRun(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "agent_runs"

    audit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("audits.id", ondelete="CASCADE"), nullable=False
    )
    agent: Mapped[AgentName] = mapped_column(pg_enum(AgentName, "agent_name"), nullable=False)
    model: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(Text)  # Langfuse


class EvalRun(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "eval_runs"

    config: Mapped[EvalConfig] = mapped_column(pg_enum(EvalConfig, "eval_config"), nullable=False)
    benchmark_version: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metrics: Mapped[dict | None] = mapped_column(JSONB)


class EvalResult(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "eval_results"

    eval_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False
    )
    repo_name: Mapped[str] = mapped_column(Text, nullable=False)
    expected_issue_id: Mapped[str] = mapped_column(Text, nullable=False)
    detected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    evidence_correct: Mapped[bool | None] = mapped_column(Boolean)
    severity_correct: Mapped[bool | None] = mapped_column(Boolean)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    latency_ms: Mapped[int | None] = mapped_column(Integer)


__all__ = [
    "EMBEDDING_DIM",
    "AgentRun",
    "Audit",
    "AuditLog",
    "CallEdge",
    "Chunk",
    "EvalResult",
    "EvalRun",
    "Evidence",
    "File",
    "Finding",
    "FindingEvent",
    "ModuleEdge",
    "Report",
    "Repository",
    "Review",
    "ReviewDecisionRow",
    "RiskAssessment",
    "RiskFactor",
    "ScannerRun",
    "Symbol",
    "User",
]
