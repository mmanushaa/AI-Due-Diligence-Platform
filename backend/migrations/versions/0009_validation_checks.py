"""record every semantic validation check

Revision ID: 0009_validation_checks
Revises: 0008_scanner_failures
Create Date: 2026-11-18

Step 2.10b replaces the loose ``claim_true``/``reason`` reply with a strict
``verdict``/``quote`` contract, and a rejection now has to be explainable from
stored data alone. Each model check therefore writes one row here: the verdict, the
quote it relied on, the full masked reply, the exact cited lines it was shown, and
what the call cost in time and money. ``eval_run_id`` is filled by the harness for
the checks a benchmark run produced.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_validation_checks"
down_revision: str | None = "0008_scanner_failures"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "validation_checks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("finding_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("findings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("eval_run_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("eval_runs.id", ondelete="SET NULL")),
        sa.Column("audit_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("audits.id", ondelete="CASCADE"), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("quote", sa.Text()),
        sa.Column("raw_reply", sa.Text()),
        sa.Column("cited_file", sa.Text()),
        sa.Column("line_start", sa.Integer()),
        sa.Column("line_end", sa.Integer()),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("cost_usd", sa.Numeric(10, 4)),
    )
    op.create_index("ix_validation_checks_finding_id", "validation_checks", ["finding_id"])
    op.create_index("ix_validation_checks_eval_run_id", "validation_checks", ["eval_run_id"])
    op.create_index("ix_validation_checks_audit_id", "validation_checks", ["audit_id"])


def downgrade() -> None:
    op.drop_index("ix_validation_checks_audit_id", table_name="validation_checks")
    op.drop_index("ix_validation_checks_eval_run_id", table_name="validation_checks")
    op.drop_index("ix_validation_checks_finding_id", table_name="validation_checks")
    op.drop_table("validation_checks")
