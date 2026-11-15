"""Drive one audit's workflow for the step 3.7 SSE demo.

    backend/.venv/Scripts/python.exe backend/scripts/demo_sse_run.py <audit_id>

The stages are stubs (no docker images, no model calls) but the node code, the commits
and the progress publishing are the real ones, and each stage sleeps briefly so the
tracker has time to show it running.
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decimal import Decimal  # noqa: E402

from app.db.enums import AgentName  # noqa: E402
from app.db.models import AgentRun  # noqa: E402
from app.db.session import get_session_factory  # noqa: E402
from app.workflow.graph import postgres_checkpointer, start_or_resume  # noqa: E402
from app.workflow.nodes import WorkflowServices  # noqa: E402

DELAY = 1.5


def services() -> WorkflowServices:
    def index(session, audit_id, snapshot):
        time.sleep(DELAY)
        return SimpleNamespace(included=18, excluded=2)

    def scanners(audit_id, snapshot, session_factory=None):
        time.sleep(DELAY)
        return SimpleNamespace(failed=())

    def agents(session, audit_id):
        # Real agent_runs rows, so the tracker's cost counter is the real sum and not
        # a number the demo made up.
        for agent, cost in ((AgentName.SECURITY, "0.0061"), (AgentName.ARCHITECTURE, "0.0094")):
            session.add(
                AgentRun(
                    audit_id=audit_id,
                    agent=agent,
                    model="agent-default",
                    input_tokens=1200,
                    output_tokens=300,
                    cost_usd=Decimal(cost),
                    status="ok",
                )
            )
        session.commit()
        time.sleep(DELAY)
        return SimpleNamespace(errors=(), accepted=3, calls=3)

    def validate(session, audit_id, semantic=True):
        time.sleep(DELAY)
        return SimpleNamespace(checked=3, verified=3, rejected=0, unresolved=0)

    return WorkflowServices(
        session_factory=get_session_factory(),
        index=index,
        scanners=scanners,
        agents=agents,
        validation=validate,
    )


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: demo_sse_run.py <audit_id>", file=sys.stderr)
        return 2
    audit_id = uuid.UUID(sys.argv[1])
    with postgres_checkpointer() as saver:
        state = start_or_resume(audit_id, services=services(), checkpointer=saver)
    print(f"run finished: status={state.get('status')} stage={state.get('stage')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
