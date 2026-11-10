"""configuration names: a tool-using single agent and a model-free graph run

Revision ID: 0004_config_names
Revises: 0003_agent_events
Create Date: 2026-11-10

Step 2.6f adds two benchmark configurations and renames one:

    single_agent              -> single_agent_no_tools   (same stage, same run)
    single_agent_tools         new   one agent, the crews' three tools and limits
    scanners_plus_deterministic new   scanners plus the graph's cycles, no model

The rename is a rename of the enum label, not a second value: existing
``eval_runs`` rows that say ``single_agent`` become ``single_agent_no_tools``
automatically, which is what keeps an old run comparable with a new one instead of
leaving two names for the same configuration.

``ALTER TYPE ... ADD VALUE`` cannot use the value it adds in the same transaction,
so this migration only adds labels; no row is written here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_config_names"
down_revision: str | None = "0003_agent_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ADDED = ("single_agent_tools", "scanners_plus_deterministic")


def upgrade() -> None:
    op.execute("ALTER TYPE eval_config RENAME VALUE 'single_agent' TO 'single_agent_no_tools'")
    for value in _ADDED:
        op.execute(f"ALTER TYPE eval_config ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    """Drop the two added labels, unless a stored run still uses one.

    Postgres cannot remove an enum label in place, so the type is rebuilt. That
    rewrites ``eval_runs.config``, which is why it refuses to run while a row uses
    a label the old type does not have: deleting that run is the operator's
    decision, not a migration's.
    """
    in_use = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM eval_runs WHERE config::text IN "
            "('single_agent_tools', 'scanners_plus_deterministic')"
        )
    ).scalar_one()
    if in_use:
        raise RuntimeError(
            f"{in_use} eval_runs row(s) use a configuration this downgrade removes; "
            "delete those runs first, or stay on 0004"
        )

    op.execute(
        "CREATE TYPE eval_config_previous AS ENUM "
        "('scanners_only', 'single_agent', 'multi_agent', 'multi_agent_validated')"
    )
    op.execute(
        "ALTER TABLE eval_runs ALTER COLUMN config TYPE eval_config_previous USING ("
        "CASE config::text WHEN 'single_agent_no_tools' THEN 'single_agent' "
        "ELSE config::text END)::eval_config_previous"
    )
    op.execute("DROP TYPE eval_config")
    op.execute("ALTER TYPE eval_config_previous RENAME TO eval_config")
