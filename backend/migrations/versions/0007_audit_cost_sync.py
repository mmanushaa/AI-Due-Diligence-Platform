"""keep audits.cost_usd equal to the sum of agent_runs

Revision ID: 0007_audit_cost_sync
Revises: 0006_append_only
Create Date: 2026-11-15

Step 3.8 asks that a run's displayed cost reconcile with what was actually spent. The
LLM client kept the two in step, but that made the invariant a property of one writer:
anything else inserting ``agent_runs`` rows - an eval harness, a repair script, a future
stage - left ``audits.cost_usd`` behind.

A trigger on ``agent_runs`` makes it a property of the data instead: on every insert,
update or delete the owning audit's cost is recomputed from the rows that exist. The
client keeps writing the column too (it is cheap and keeps the ORM object honest after
its own commit); this is the guarantee underneath it.

The migration also backfills, so audits written before the trigger reconcile too.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_audit_cost_sync"
down_revision: str | None = "0006_append_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FUNCTION = """
CREATE OR REPLACE FUNCTION sync_audit_cost_usd() RETURNS trigger AS $$
DECLARE
    target uuid;
BEGIN
    target := COALESCE(NEW.audit_id, OLD.audit_id);
    UPDATE audits
       SET cost_usd = COALESCE(
           (SELECT SUM(cost_usd) FROM agent_runs WHERE audit_id = target), 0
       )
     WHERE id = target;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""

BACKFILL = """
UPDATE audits a
   SET cost_usd = COALESCE(
       (SELECT SUM(r.cost_usd) FROM agent_runs r WHERE r.audit_id = a.id), 0
   );
"""


def upgrade() -> None:
    op.execute(FUNCTION)
    op.execute(BACKFILL)
    op.execute(
        "CREATE TRIGGER agent_runs_sync_audit_cost "
        "AFTER INSERT OR UPDATE OR DELETE ON agent_runs "
        "FOR EACH ROW EXECUTE FUNCTION sync_audit_cost_usd()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS agent_runs_sync_audit_cost ON agent_runs")
    op.execute("DROP FUNCTION IF EXISTS sync_audit_cost_usd()")
