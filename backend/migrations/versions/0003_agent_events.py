"""agent events: what a crew did while it was working

Revision ID: 0003_agent_events
Revises: 0002_eval_instrumentation
Create Date: 2026-11-09

Step 2.6e. ``agent_runs`` records that a call happened and what it cost; it cannot
say which tool the model asked for, what a guardrail refused, or what the model
actually wrote when it was refused. Those three are what a failed crew is
diagnosed from, so ``agent_events`` stores one row per occurrence:

    tool_call          tool_name, arguments, size of the result
    guardrail_failure  the refused reply, the feedback, the attempt number
    final_output       the raw text each task ended with

``payload`` is JSONB because the three kinds carry different fields, and every
string in it has been through the scanners' secret masker before it was stored.

The audit owns the rows: ON DELETE CASCADE, like every other per-audit table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_agent_events"
down_revision: str | None = "0002_eval_instrumentation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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


def upgrade() -> None:
    # The type is created once here; create_type=False keeps create_table from
    # trying to create it a second time.
    op.execute(
        "CREATE TYPE agent_event_type AS ENUM "
        "('tool_call', 'guardrail_failure', 'final_output')"
    )
    event_type = postgresql.ENUM(
        "tool_call",
        "guardrail_failure",
        "final_output",
        name="agent_event_type",
        create_type=False,
    )
    # Already created by 0001; the column only references it.
    agent_name = postgresql.ENUM(name="agent_name", create_type=False)

    op.create_table(
        "agent_events",
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent", agent_name, nullable=False),
        sa.Column("event_type", event_type, nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        _id(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["audit_id"],
            ["audits.id"],
            name=op.f("fk_agent_events_audit_id_audits"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_events")),
    )
    op.create_index(op.f("ix_agent_events_audit_id"), "agent_events", ["audit_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_events_audit_id"), table_name="agent_events")
    op.drop_table("agent_events")
    op.execute("DROP TYPE agent_event_type")
