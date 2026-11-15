"""The audit workflow, as a LangGraph graph with a Postgres checkpointer (step 3.2).

LangGraph owns the order: intake, index, scanners, route, agents, validation, risk,
finalize, and the two stub nodes review and report. CrewAI lives *inside* the agents
node - the graph knows a node named "agents", not a crew - so a crew change cannot
change the order of stages, and a resumed run cannot restart a crew halfway.

The checkpointer is what makes a killed worker recoverable. Every node's return value
is written to the checkpointer's tables under the audit's thread
(``audits.workflow_thread_id``), so a restarted process starts at the last node that
finished rather than at intake. The tables live in their own Postgres schema,
``workflow``, so a checkpoint write can never collide with the application's own
migrations or be dropped by a table-rebuild migration.

Resuming is the same call as starting: :func:`start_or_resume` looks at whether the
thread has a pending next node and either continues it or starts a new run.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.core.config import settings
from app.db.models import Audit
from app.db.session import get_session_factory
from app.workflow.nodes import (
    NODES,
    WorkflowServices,
    agents,
    finalize,
    index,
    intake,
    report,
    review,
    risk,
    route,
    scanners,
    validation,
)
from app.workflow.state import AuditState

logger = logging.getLogger(__name__)

#: The checkpointer's own schema. Not the application's: a checkpoint table has a
#: different lifecycle from the audit tables and must never be migrated with them.
WORKFLOW_SCHEMA = "workflow"


def checkpointer_dsn(database_url: str | None = None, schema: str = WORKFLOW_SCHEMA) -> str:
    """A plain psycopg DSN that puts the checkpointer in its own schema."""
    url = (database_url or settings.database_url).replace("postgresql+psycopg://", "postgresql://")
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query))
    existing = query.get("options", "")
    option = f"-csearch_path={schema}"
    query["options"] = f"{existing} {option}".strip() if existing else option
    return urlunparse(parts._replace(query=urlencode(query)))


def _ensure_schema(dsn: str, schema: str = WORKFLOW_SCHEMA) -> None:
    """Create the checkpointer's schema before its tables are set up."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")


@contextmanager
def postgres_checkpointer(
    dsn: str | None = None, schema: str = WORKFLOW_SCHEMA
) -> Iterator[PostgresSaver]:
    """A ready checkpointer, its schema and tables created on first use."""
    target = dsn or checkpointer_dsn(schema=schema)
    _ensure_schema(target, schema)
    with PostgresSaver.from_conn_string(target) as saver:
        saver.setup()
        yield saver


def _bind(node: Callable[..., dict[str, Any]], services: WorkflowServices) -> Callable[[AuditState], dict[str, Any]]:
    """Give LangGraph a one-argument node; the services stay injected."""

    def bound(state: AuditState) -> dict[str, Any]:
        return node(state, services)

    bound.__name__ = node.__name__
    return bound


def build_graph(checkpointer: Any = None, services: WorkflowServices | None = None) -> Any:
    """Compile the workflow. Without a checkpointer it is a plain in-memory graph."""
    chosen = services or WorkflowServices()
    builder = StateGraph(AuditState)
    for name, node in NODES.items():
        builder.add_node(name, _bind(node, chosen))

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "index")
    builder.add_edge("index", "scanners")
    builder.add_edge("scanners", "route")
    builder.add_conditional_edges(
        "route",
        lambda state: state.route,
        {"agents": "agents", "skip": "risk"},
    )
    builder.add_conditional_edges(
        "agents",
        lambda state: "stop" if state.budget_exceeded else "validation",
        {"validation": "validation", "stop": "finalize"},
    )
    builder.add_edge("validation", "risk")
    # The review gate (step 3.4): only a critical audit is routed to a human.
    builder.add_conditional_edges(
        "risk",
        lambda state: "review" if state.risk_level == "critical" else "finalize",
        {"review": "review", "finalize": "finalize"},
    )
    # A request for evidence sends the run back to validation with what it learned.
    builder.add_conditional_edges(
        "review",
        lambda state: (
            "validation" if state.review_decision == "request_evidence" else "finalize"
        ),
        {"validation": "validation", "finalize": "finalize"},
    )
    builder.add_edge("finalize", "report")
    builder.add_edge("report", END)
    return builder.compile(checkpointer=checkpointer)


def thread_id_for(session_factory: Callable[[], Any] | None = None, audit_id: uuid.UUID | None = None) -> str:
    """The audit's thread id, stored on first use (``audits.workflow_thread_id``)."""
    factory = session_factory or get_session_factory()
    session = factory()
    try:
        audit = session.get(Audit, audit_id)
        if audit is None:
            raise ValueError(f"audit {audit_id} does not exist")
        if not audit.workflow_thread_id:
            audit.workflow_thread_id = str(audit_id)
            session.commit()
        return audit.workflow_thread_id
    finally:
        session.close()


def run_configuration(audit_id: uuid.UUID, thread: str | None = None) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread or str(audit_id)}}


def start_or_resume(
    audit_id: uuid.UUID,
    *,
    services: WorkflowServices | None = None,
    checkpointer: Any = None,
    dsn: str | None = None,
    session_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Start the workflow for this audit, or continue it if a run already exists.

    This is the whole Celery task: it never runs a stage itself. A redelivered
    message after a worker death finds the thread with a pending node and continues
    from it, because the checkpointer kept everything the finished nodes produced.
    """
    factory = (
        session_factory
        or (services.session_factory if services else None)
        or get_session_factory()
    )
    thread = thread_id_for(factory, audit_id)
    config = run_configuration(audit_id, thread)

    def run(saver: Any) -> dict[str, Any]:
        graph = build_graph(saver, services)
        snapshot = graph.get_state(config)
        if snapshot.next:
            logger.info(
                "resuming audit %s at %s (thread %s)",
                audit_id,
                ", ".join(snapshot.next),
                thread,
            )
            return graph.invoke(None, config)
        if snapshot.values and "report" in (snapshot.values.get("completed") or ()):
            # The thread already finished. A message redelivered after the last
            # checkpoint must not re-run the whole audit.
            logger.info("audit %s already finished on thread %s", audit_id, thread)
            return snapshot.values
        logger.info("starting audit %s (thread %s)", audit_id, thread)
        return graph.invoke({"audit_id": str(audit_id)}, config)

    if checkpointer is not None:
        return run(checkpointer)
    with postgres_checkpointer(dsn) as saver:
        return run(saver)


def resume_workflow(
    audit_id: uuid.UUID,
    value: Any = None,
    *,
    services: WorkflowServices | None = None,
    checkpointer: Any = None,
    dsn: str | None = None,
) -> dict[str, Any]:
    """Continue a paused run, handing the node that interrupted the resume value.

    Used by the review API after it records a decision: the review node reads the
    decision back from the database, so the value here only has to arrive.
    """
    thread = thread_id_for(
        (services.session_factory if services else None) or get_session_factory(),
        audit_id,
    )
    config = run_configuration(audit_id, thread)

    def run(saver: Any) -> dict[str, Any]:
        graph = build_graph(saver, services)
        logger.info("resuming audit %s with a review decision", audit_id)
        return graph.invoke(Command(resume=value), config)

    if checkpointer is not None:
        return run(checkpointer)
    with postgres_checkpointer(dsn) as saver:
        return run(saver)


def pending_nodes(audit_id: uuid.UUID, *, dsn: str | None = None, checkpointer: Any = None) -> list[str]:
    """What a restart would do next; empty when the thread is finished."""
    config = run_configuration(audit_id)
    if checkpointer is not None:
        return list(build_graph(checkpointer).get_state(config).next)
    with postgres_checkpointer(dsn) as saver:
        return list(build_graph(saver).get_state(config).next)


__all__ = [
    "NODES",
    "WORKFLOW_SCHEMA",
    "build_graph",
    "checkpointer_dsn",
    "pending_nodes",
    "postgres_checkpointer",
    "resume_workflow",
    "start_or_resume",
    "thread_id_for",
]
