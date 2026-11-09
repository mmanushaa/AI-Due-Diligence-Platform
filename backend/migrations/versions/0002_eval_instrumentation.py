"""eval instrumentation: matched finding columns and the extras table

Revision ID: 0002_eval_instrumentation
Revises: 0001_initial_schema
Create Date: 2026-11-09

Step 2.6b makes the harness record *which* finding satisfied each expected
issue, or why it did not. ``eval_results`` therefore gains the identity and
location of the finding it matched, plus ``is_cross_file`` and ``miss_reason``.

Findings that matched no expected issue are kept in ``eval_extras`` rather than
being discarded, so a report can name every extra with its real file, lines and
a rejection reason instead of recounting it from memory.

``matched_finding_id`` and ``eval_extras.finding_id`` use ON DELETE SET NULL:
the eval rows must survive the audit prune that deletes their findings, or the
numbers would vanish with them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_eval_instrumentation"
down_revision: str | None = "0001_initial_schema"
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
    op.add_column(
        "eval_results",
        sa.Column("matched_finding_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("eval_results", sa.Column("matched_origin", sa.Text(), nullable=True))
    op.add_column("eval_results", sa.Column("matched_source_tool", sa.Text(), nullable=True))
    op.add_column("eval_results", sa.Column("matched_file", sa.Text(), nullable=True))
    op.add_column("eval_results", sa.Column("matched_line_start", sa.Integer(), nullable=True))
    op.add_column("eval_results", sa.Column("matched_line_end", sa.Integer(), nullable=True))
    op.add_column("eval_results", sa.Column("is_cross_file", sa.Boolean(), nullable=True))
    op.add_column("eval_results", sa.Column("miss_reason", sa.Text(), nullable=True))

    # The finding itself may be pruned with its audit; the result row stays.
    op.create_foreign_key(
        op.f("fk_eval_results_matched_finding_id_findings"),
        "eval_results",
        "findings",
        ["matched_finding_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "eval_extras",
        sa.Column("eval_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("finding_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("file", sa.Text(), nullable=True),
        sa.Column("line_start", sa.Integer(), nullable=True),
        sa.Column("line_end", sa.Integer(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=True),
        sa.Column("source_tool", sa.Text(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        _id(),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["eval_run_id"],
            ["eval_runs.id"],
            name=op.f("fk_eval_extras_eval_run_id_eval_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["findings.id"],
            name=op.f("fk_eval_extras_finding_id_findings"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_extras")),
    )


def downgrade() -> None:
    op.drop_table("eval_extras")
    op.drop_constraint(
        op.f("fk_eval_results_matched_finding_id_findings"),
        "eval_results",
        type_="foreignkey",
    )
    for column in (
        "miss_reason",
        "is_cross_file",
        "matched_line_end",
        "matched_line_start",
        "matched_file",
        "matched_source_tool",
        "matched_origin",
        "matched_finding_id",
    ):
        op.drop_column("eval_results", column)
