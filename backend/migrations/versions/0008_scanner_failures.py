"""record scanner failures so a report can state the gap

Revision ID: 0008_scanner_failures
Revises: 0007_audit_cost_sync
Create Date: 2026-11-15

Step 3.9's limitations section has to list the scanners that did not complete. Until
now a failure lived only in the workflow's in-memory state, which a report generated
later cannot see.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_scanner_failures"
down_revision: str | None = "0007_audit_cost_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scanner_failures",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("audit_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("audits.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
    )
    op.create_index("ix_scanner_failures_audit_id", "scanner_failures", ["audit_id"])


def downgrade() -> None:
    op.drop_index("ix_scanner_failures_audit_id", table_name="scanner_failures")
    op.drop_table("scanner_failures")
