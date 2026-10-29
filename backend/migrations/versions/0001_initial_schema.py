"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-10-29

Creates every table and enum from docs/5. Backend Schema.md, the pgvector
embedding column, and the indexes listed in the Indexes section. Append-only
triggers are deliberately not part of this migration (step 3.5).

Enum types are created once up front: several of them (severity,
verification_status) are shared by more than one table, so letting each
create_table() emit ``CREATE TYPE`` would fail on the second use.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 1536

ENUMS: dict[str, list[str]] = {
    "user_role": ["analyst", "reviewer", "admin"],
    "source_type": ["zip", "git_url"],
    "audit_status": [
        "queued",
        "running",
        "awaiting_review",
        "needs_evidence",
        "completed",
        "failed",
        "cancelled",
    ],
    "stage": [
        "intake",
        "index",
        "scanners",
        "agents",
        "validation",
        "risk",
        "review",
        "report",
    ],
    "severity": ["info", "low", "medium", "high", "critical"],
    "category": ["security", "architecture", "license", "dependency", "secret"],
    "verification_status": [
        "scanner_confirmed",
        "ai_suggested",
        "verified",
        "rejected_false_positive",
        "unresolved",
    ],
    "verdict": ["issue", "no_issue", "insufficient_evidence"],
    "finding_origin": ["scanner", "agent"],
    "symbol_kind": ["function", "class", "method", "route", "import", "config"],
    "scanner_tool": ["gitleaks", "semgrep", "osv", "scancode", "ast"],
    "scanner_run_status": ["pending", "running", "succeeded", "failed", "skipped"],
    "evidence_kind": ["primary", "supporting", "retrieved_context"],
    "assessment_status": ["draft", "pending_review", "final"],
    "review_status": ["pending", "approved", "overridden", "needs_evidence"],
    "review_decision": ["approve", "override", "request_evidence"],
    "agent_name": ["security", "architecture", "compliance", "reporter"],
    "eval_config": [
        "scanners_only",
        "single_agent",
        "multi_agent",
        "multi_agent_validated",
    ],
}


def _id() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        server_default=sa.text("gen_random_uuid()"),
        nullable=False,
    )


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def _enum(name: str) -> postgresql.ENUM:
    """Reference an already created enum type."""
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    for name, values in ENUMS.items():
        values_sql = ", ".join(f"'{value}'" for value in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({values_sql})")

    op.create_table(
        "users",
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", _enum("user_role"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        _id(),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )

    op.create_table(
        "repositories",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source_type", _enum("source_type"), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("storage_path", sa.Text(), nullable=True),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        _id(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["uploaded_by"],
            ["users.id"],
            name=op.f("fk_repositories_uploaded_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repositories")),
    )

    op.create_table(
        "audits",
        sa.Column("repository_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", _enum("audit_status"), nullable=False),
        sa.Column("current_stage", _enum("stage"), nullable=True),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("workflow_thread_id", sa.Text(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(10, 4), server_default=sa.text("0"), nullable=False),
        sa.Column("budget_usd", sa.Numeric(10, 4), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_by", postgresql.UUID(as_uuid=True), nullable=True),
        _id(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_audits_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["started_by"],
            ["users.id"],
            name=op.f("fk_audits_started_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audits")),
    )

    op.create_table(
        "files",
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.Text(), nullable=True),
        sa.Column("is_excluded", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("exclude_reason", sa.Text(), nullable=True),
        _id(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["audit_id"],
            ["audits.id"],
            name=op.f("fk_files_audit_id_audits"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_files")),
    )

    op.create_table(
        "symbols",
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", _enum("symbol_kind"), nullable=False),
        sa.Column("line_start", sa.Integer(), nullable=False),
        sa.Column("line_end", sa.Integer(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        _id(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["audit_id"],
            ["audits.id"],
            name=op.f("fk_symbols_audit_id_audits"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["files.id"],
            name=op.f("fk_symbols_file_id_files"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_symbols")),
    )

    op.create_table(
        "chunks",
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("symbol_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("line_start", sa.Integer(), nullable=False),
        sa.Column("line_end", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_tsv", postgresql.TSVECTOR(), nullable=True),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
        _id(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["audit_id"],
            ["audits.id"],
            name=op.f("fk_chunks_audit_id_audits"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["files.id"],
            name=op.f("fk_chunks_file_id_files"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["symbol_id"],
            ["symbols.id"],
            name=op.f("fk_chunks_symbol_id_symbols"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunks")),
    )

    op.create_table(
        "call_edges",
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("caller_symbol_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("callee_symbol_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("callee_name", sa.Text(), nullable=False),
        _id(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["audit_id"],
            ["audits.id"],
            name=op.f("fk_call_edges_audit_id_audits"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["callee_symbol_id"],
            ["symbols.id"],
            name=op.f("fk_call_edges_callee_symbol_id_symbols"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["caller_symbol_id"],
            ["symbols.id"],
            name=op.f("fk_call_edges_caller_symbol_id_symbols"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_call_edges")),
    )

    op.create_table(
        "module_edges",
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("to_file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        _id(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["audit_id"],
            ["audits.id"],
            name=op.f("fk_module_edges_audit_id_audits"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["from_file_id"],
            ["files.id"],
            name=op.f("fk_module_edges_from_file_id_files"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["to_file_id"],
            ["files.id"],
            name=op.f("fk_module_edges_to_file_id_files"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_module_edges")),
    )

    op.create_table(
        "scanner_runs",
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tool", _enum("scanner_tool"), nullable=False),
        sa.Column("version", sa.Text(), nullable=True),
        sa.Column("status", _enum("scanner_run_status"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("raw_output_path", sa.Text(), nullable=True),
        _id(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["audit_id"],
            ["audits.id"],
            name=op.f("fk_scanner_runs_audit_id_audits"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scanner_runs")),
    )

    op.create_table(
        "findings",
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category", _enum("category"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("severity", _enum("severity"), nullable=False),
        sa.Column("confidence", sa.Numeric(3, 2), nullable=True),
        sa.Column("origin", _enum("finding_origin"), nullable=False),
        sa.Column("source_tool", sa.Text(), nullable=True),
        sa.Column("verification_status", _enum("verification_status"), nullable=False),
        sa.Column("verdict", _enum("verdict"), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("business_impact", sa.Text(), nullable=True),
        sa.Column("recommendation", sa.Text(), nullable=True),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        _id(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["audit_id"],
            ["audits.id"],
            name=op.f("fk_findings_audit_id_audits"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_findings")),
        sa.UniqueConstraint(
            "audit_id", "fingerprint", name=op.f("uq_findings_audit_fingerprint")
        ),
    )

    op.create_table(
        "evidence",
        sa.Column("finding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("line_start", sa.Integer(), nullable=False),
        sa.Column("line_end", sa.Integer(), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("kind", _enum("evidence_kind"), nullable=False),
        sa.Column("retrieval_score", sa.Numeric(10, 4), nullable=True),
        _id(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["files.id"],
            name=op.f("fk_evidence_file_id_files"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["findings.id"],
            name=op.f("fk_evidence_finding_id_findings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence")),
    )

    op.create_table(
        "finding_events",
        sa.Column("finding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_status", _enum("verification_status"), nullable=True),
        sa.Column("to_status", _enum("verification_status"), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        _id(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["findings.id"],
            name=op.f("fk_finding_events_finding_id_findings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_finding_events")),
    )

    op.create_table(
        "risk_assessments",
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("overall_level", _enum("severity"), nullable=False),
        sa.Column("overall_score", sa.Numeric(10, 4), nullable=True),
        sa.Column("rule_applied", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("technical_summary", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("business_risk_note", sa.Text(), nullable=True),
        sa.Column("status", _enum("assessment_status"), nullable=False),
        sa.Column("supersedes_id", postgresql.UUID(as_uuid=True), nullable=True),
        _id(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["audit_id"],
            ["audits.id"],
            name=op.f("fk_risk_assessments_audit_id_audits"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["risk_assessments.id"],
            name=op.f("fk_risk_assessments_supersedes_id_risk_assessments"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_assessments")),
    )

    op.create_table(
        "risk_factors",
        sa.Column("assessment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("finding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("severity_weight", sa.Numeric(10, 4), nullable=False),
        sa.Column("evidence_confidence", sa.Numeric(10, 4), nullable=False),
        sa.Column("exposure_multiplier", sa.Numeric(10, 4), nullable=False),
        sa.Column("finding_score", sa.Numeric(10, 4), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        _id(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            ["risk_assessments.id"],
            name=op.f("fk_risk_factors_assessment_id_risk_assessments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["findings.id"],
            name=op.f("fk_risk_factors_finding_id_findings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_factors")),
    )

    op.create_table(
        "reviews",
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assessment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", _enum("review_status"), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        _id(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            ["risk_assessments.id"],
            name=op.f("fk_reviews_assessment_id_risk_assessments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["audit_id"],
            ["audits.id"],
            name=op.f("fk_reviews_audit_id_audits"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reviews")),
    )

    op.create_table(
        "review_decisions",
        sa.Column("review_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reviewer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decision", _enum("review_decision"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("original_level", _enum("severity"), nullable=True),
        sa.Column("final_level", _enum("severity"), nullable=True),
        _id(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["reviews.id"],
            name=op.f("fk_review_decisions_review_id_reviews"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_id"],
            ["users.id"],
            name=op.f("fk_review_decisions_reviewer_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_decisions")),
    )

    op.create_table(
        "reports",
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("html", sa.Text(), nullable=True),
        sa.Column("pdf_path", sa.Text(), nullable=True),
        sa.Column("generated_by", sa.Text(), nullable=True),
        sa.Column("assessment_id", postgresql.UUID(as_uuid=True), nullable=True),
        _id(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            ["risk_assessments.id"],
            name=op.f("fk_reports_assessment_id_risk_assessments"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["audit_id"],
            ["audits.id"],
            name=op.f("fk_reports_audit_id_audits"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reports")),
    )

    op.create_table(
        "audit_log",
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("object_type", sa.Text(), nullable=False),
        sa.Column("object_id", sa.Text(), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        _id(),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )

    op.create_table(
        "agent_runs",
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent", _enum("agent_name"), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(10, 4), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=True),
        _id(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["audit_id"],
            ["audits.id"],
            name=op.f("fk_agent_runs_audit_id_audits"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")),
    )

    op.create_table(
        "eval_runs",
        sa.Column("config", _enum("eval_config"), nullable=False),
        sa.Column("benchmark_version", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        _id(),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_runs")),
    )

    op.create_table(
        "eval_results",
        sa.Column("eval_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("repo_name", sa.Text(), nullable=False),
        sa.Column("expected_issue_id", sa.Text(), nullable=False),
        sa.Column("detected", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("evidence_correct", sa.Boolean(), nullable=True),
        sa.Column("severity_correct", sa.Boolean(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(10, 4), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        _id(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["eval_run_id"],
            ["eval_runs.id"],
            name=op.f("fk_eval_results_eval_run_id_eval_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_results")),
    )

    # Lookup and dedupe indexes.
    op.create_index("ix_files_audit_id_path", "files", ["audit_id", "path"])
    op.create_index("ix_findings_audit_id_severity", "findings", ["audit_id", "severity"])
    op.create_index(
        "ix_findings_audit_id_verification_status",
        "findings",
        ["audit_id", "verification_status"],
    )
    op.create_index("ix_evidence_finding_id", "evidence", ["finding_id"])

    # Lexical search: full-text on chunks, trigram on symbol names.
    op.create_index(
        "ix_chunks_content_tsv", "chunks", ["content_tsv"], postgresql_using="gin"
    )
    op.create_index(
        "ix_symbols_name_trgm",
        "symbols",
        ["name"],
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )

    # Vector search: cosine distance over the embedding column.
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    # Graph traversal.
    op.create_index("ix_call_edges_caller_symbol_id", "call_edges", ["caller_symbol_id"])
    op.create_index("ix_call_edges_callee_symbol_id", "call_edges", ["callee_symbol_id"])
    op.create_index("ix_module_edges_from_file_id", "module_edges", ["from_file_id"])


def downgrade() -> None:
    op.drop_index("ix_module_edges_from_file_id", table_name="module_edges")
    op.drop_index("ix_call_edges_callee_symbol_id", table_name="call_edges")
    op.drop_index("ix_call_edges_caller_symbol_id", table_name="call_edges")
    op.drop_index("ix_chunks_embedding_hnsw", table_name="chunks")
    op.drop_index("ix_symbols_name_trgm", table_name="symbols")
    op.drop_index("ix_chunks_content_tsv", table_name="chunks")
    op.drop_index("ix_evidence_finding_id", table_name="evidence")
    op.drop_index("ix_findings_audit_id_verification_status", table_name="findings")
    op.drop_index("ix_findings_audit_id_severity", table_name="findings")
    op.drop_index("ix_files_audit_id_path", table_name="files")

    for table in (
        "eval_results",
        "eval_runs",
        "agent_runs",
        "audit_log",
        "reports",
        "review_decisions",
        "reviews",
        "risk_factors",
        "risk_assessments",
        "finding_events",
        "evidence",
        "findings",
        "scanner_runs",
        "module_edges",
        "call_edges",
        "chunks",
        "symbols",
        "files",
        "audits",
        "repositories",
        "users",
    ):
        op.drop_table(table)

    for name in reversed(list(ENUMS)):
        op.execute(f"DROP TYPE IF EXISTS {name}")
