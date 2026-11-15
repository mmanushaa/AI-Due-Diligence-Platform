"""Step 3.8 demo: one traced audit that runs out of budget.

    LANGFUSE_HOST=http://localhost:3000 LANGFUSE_PUBLIC_KEY=pk-lf-local-dev \
    LANGFUSE_SECRET_KEY=sk-lf-local-dev \
    backend/.venv/Scripts/python.exe backend/scripts/demo_trace_and_budget.py

The agents stage makes real LLMClient calls (through a local provider object, because
no provider key is configured here) so the tokens, cost and latency in the trace are
produced by the same code a live run uses. The budget is small on purpose: the fourth
call is refused before it reaches the provider, the run ends `needs_evidence`, and the
script then reads the trace back from Langfuse and reconciles the cost.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.request
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select, text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.enums import AuditStatus, SourceType  # noqa: E402
from app.db.models import AgentRun, Audit, Repository  # noqa: E402
from app.db.session import get_session_factory  # noqa: E402
from app.llm.client import BudgetExceeded, Completion, LLMClient  # noqa: E402
from app.observability.tracing import get_tracer, trace_id_for  # noqa: E402
from app.workflow.graph import postgres_checkpointer, start_or_resume  # noqa: E402
from app.workflow.nodes import WorkflowServices  # noqa: E402

BUDGET = Decimal("0.001")
AGENTS = ("security", "architecture", "compliance")


class OfflineProvider:
    """A provider with no network: fixed usage, no reported cost."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, model, messages, max_tokens, temperature, tools=None):
        self.calls += 1
        time.sleep(0.05)  # so latency in the trace is a real measurement
        return Completion(
            text=f"analysis {self.calls}",
            input_tokens=1_800,
            output_tokens=450,
            # No reported cost, so the client prices the call from its table exactly
            # as it would for a provider that does not return one.
            reported_cost_usd=None,
        )


def seed_audit(session) -> tuple[uuid.UUID, uuid.UUID]:
    tmp = Path(tempfile.mkdtemp(prefix="demo-trace-"))
    (tmp / "app").mkdir()
    (tmp / "app" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    repository = Repository(
        name=f"demo-trace-{uuid.uuid4().hex[:6]}",
        source_type=SourceType.ZIP,
        storage_path=str(tmp),
    )
    session.add(repository)
    session.commit()
    session.refresh(repository)
    audit = Audit(repository_id=repository.id, status=AuditStatus.QUEUED)
    audit.budget_usd = BUDGET
    session.add(audit)
    session.commit()
    session.refresh(audit)
    return audit.id, repository.id


def services(provider: OfflineProvider) -> WorkflowServices:
    def index(session, audit_id, snapshot):
        return SimpleNamespace(included=1, excluded=0)

    def scanners(audit_id, snapshot, session_factory=None):
        return SimpleNamespace(failed=())

    def agents(session, audit_id):
        client = LLMClient(session, audit_id, provider=provider)
        for agent in AGENTS:
            client.complete(agent, [{"role": "user", "content": f"review {agent}"}])
        return SimpleNamespace(errors=(), accepted=3, calls=4)

    def validate(session, audit_id, semantic=True):
        return SimpleNamespace(checked=0, verified=0, rejected=0, unresolved=0)

    return WorkflowServices(
        session_factory=get_session_factory(),
        index=index,
        scanners=scanners,
        agents=agents,
        validation=validate,
    )


def langfuse_get(path: str) -> dict:
    public = settings.langfuse_public_key.get_secret_value() if settings.langfuse_public_key else ""
    secret = settings.langfuse_secret_key.get_secret_value() if settings.langfuse_secret_key else ""
    host = (settings.langfuse_host or "").rstrip("/")
    request = urllib.request.Request(f"{host}{path}")
    import base64

    token = base64.b64encode(f"{public}:{secret}".encode()).decode()
    request.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read() or b"{}")


def main() -> int:
    tracer = get_tracer()
    print(f"tracer enabled: {tracer.enabled} (host {settings.langfuse_host})")
    if not tracer.enabled:
        print("set LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY", file=sys.stderr)
        return 2

    session = get_session_factory()()
    audit_id, repository_id = seed_audit(session)
    provider = OfflineProvider()
    print(f"audit {audit_id}, budget ${BUDGET}")
    print("running workflow ...")
    try:
        with postgres_checkpointer() as saver:
            state = start_or_resume(audit_id, services=services(provider), checkpointer=saver)
    finally:
        session.close()

    trace_id = trace_id_for(audit_id)
    print(f"trace id {trace_id}")

    session = get_session_factory()()
    try:
        audit = session.get(Audit, audit_id)
        spent = session.execute(
            select(func.coalesce(func.sum(AgentRun.cost_usd), 0)).where(AgentRun.audit_id == audit_id)
        ).scalar_one()
        calls = session.execute(
            select(func.count()).select_from(AgentRun).where(AgentRun.audit_id == audit_id)
        ).scalar_one()
        trace_ids = session.execute(
            select(AgentRun.trace_id).where(AgentRun.audit_id == audit_id)
        ).scalars().all()
        print("\n--- the run ---")
        print(f"status      : {audit.status.value}")
        print(f"finished_at : {audit.finished_at}")
        print(f"incomplete  : {state.get('incomplete_reason')}")
        print(f"audits.cost_usd      : {audit.cost_usd}")
        print(f"SUM(agent_runs.cost) : {spent}")
        print(f"agent calls          : {calls} (provider actually served {provider.calls})")
        print(f"agent_runs.trace_id  : {sorted(set(trace_ids))}")
        print(f"match                : {audit.cost_usd == spent}")
    finally:
        session.close()

    tracer.flush()
    time.sleep(4)  # the SDK batches; give ingestion a moment

    print("\n--- the trace in Langfuse ---")
    try:
        trace = langfuse_get(f"/api/public/traces/{trace_id}")
        print(
            f"trace {trace.get('id')} name={trace.get('name')} "
            f"latency={trace.get('latency')}s totalCost=${trace.get('totalCost')}"
        )
        print(f"metadata: {json.dumps(trace.get('metadata'), default=str)[:300]}")
        observations = langfuse_get(f"/api/public/observations?traceId={trace_id}&limit=20")
        print(f"{'name':<14}{'model':<16}{'in':>6}{'out':>6}{'cost':>9}{'latency':>10}  level")
        for obs in observations.get("data", []):
            usage = obs.get("usage") or {}
            meta_cost = (obs.get("metadata") or {}).get("cost_usd")
            print(
                f"{obs.get('name',''):<14}{obs.get('model') or '-':<16}"
                f"{usage.get('input', 0):>6}{usage.get('output', 0):>6}"
                f"{obs.get('calculatedTotalCost') or 0:>9}"
                f"{round(obs.get('latency') or 0, 3):>10}  {obs.get('level')}"
                f"  metadata.cost_usd={meta_cost}"
            )
    except Exception as exc:  # noqa: BLE001
        print(f"could not read the trace back: {exc}", file=sys.stderr)

    if "--keep" in sys.argv:
        print(f"kept audit {audit_id} for the reconciliation query")
        return 0

    session = get_session_factory()()
    session.execute(text("ALTER TABLE review_decisions DISABLE TRIGGER USER"))
    session.delete(session.get(Audit, audit_id))
    session.commit()
    session.execute(text("ALTER TABLE review_decisions ENABLE TRIGGER USER"))
    session.delete(session.get(Repository, repository_id))
    session.commit()
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
