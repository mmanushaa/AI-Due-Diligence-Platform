"""an agent name for the validation pass

Revision ID: 0005_validator_agent
Revises: 0004_config_names
Create Date: 2026-11-13

Step 2.10 adds a validation pass that can ask a model whether cited code supports a
claim. Its calls are budgeted and logged like every other agent's, which means
``agent_runs.agent`` needs a label for them; ``validator`` also names the actor on
the ``finding_events`` rows the pass writes.

``ALTER TYPE ... ADD VALUE`` cannot use the value it adds in the same transaction,
so this migration only adds the label; no row is written here.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_validator_agent"
down_revision: str | None = "0004_config_names"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE agent_name ADD VALUE IF NOT EXISTS 'validator'")


def downgrade() -> None:
    """Refuse: Postgres cannot remove an enum label in place.

    Rebuilding the type would rewrite ``agent_runs.agent``, and the migration would
    have to guess what an existing ``validator`` row should become. An operator who
    wants that can rebuild the type themselves; a migration should not.
    """
    raise RuntimeError(
        "agent_name keeps the 'validator' label; downgrading past 0005 needs a "
        "manual enum rebuild"
    )
