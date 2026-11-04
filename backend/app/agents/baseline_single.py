"""The single-agent baseline (step 2.7).

One reviewer, one prompt, one reply: assemble context with the read-only tools,
ask the model to review it, validate the JSON against both the schema and the
repository, and store what survives as ``origin=agent``,
``verification_status=ai_suggested``.

Why one call instead of a tool-calling loop: this is the baseline every later
configuration is measured against, so it has to be cheap and repeatable. The
retrieval plan below is deterministic, which means a difference between two runs
comes from the model or the code, never from which tools it happened to call.
Step 3 introduces agents that choose their own tools, and the harness compares
them against this.

Failure handling, in order:

    invalid JSON or schema      rejected, retried once with the reasons, then dropped
    valid JSON, unverifiable    same: a citation to a line that does not exist is
                                invalid output, not a finding
    provider unreachable        the outcome records it and the audit carries on with
                                the scanner findings only
    budget exhausted            same, and the spend so far is already logged
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.prompts import SYSTEM_PROMPT, build_user_message
from app.agents.schema import (
    AgentFinding,
    InvalidOutput,
    describe_errors,
    parse_report,
    validate_report,
)
from app.agents.tools import DataBlock, RepositoryTools
from app.indexing.service import index_audit, index_graph
from app.intake.filtering import index_snapshot
from app.db.enums import (
    AgentName,
    Category,
    EvidenceKind,
    FindingOrigin,
    Severity,
    Verdict,
    VerificationStatus,
)
from app.db.models import Chunk, Evidence, File, Finding
from app.llm.client import BudgetExceeded, LLMClient, LLMError
from app.retrieval.embeddings import embed_chunks
from app.scanners.common import MAX_SNIPPET_CHARS, existing_fingerprints, finding_fingerprint, redact_text

logger = logging.getLogger(__name__)

SOURCE_TOOL = "baseline_single"
AGENT_ROLE = AgentName.SECURITY

# Deterministic retrieval plan: the four questions the baseline asks of the index.
REVIEW_QUERIES = (
    "route handler that does not check authentication or ownership",
    "SQL statement built with string formatting or template interpolation",
    "circular import between two modules",
    "copyleft GPL license in a dependency manifest",
)
MAX_BLOCKS = 24
MAX_CONTEXT_CHARS = 24_000
MAX_REPLY_TOKENS = 2_048
RETRIES = 1


@dataclass(frozen=True)
class AgentOutcome:
    """What one agent stage did, whether or not it succeeded."""

    stage: str = SOURCE_TOOL
    model: str | None = None
    calls: int = 0
    accepted: int = 0
    rejected: int = 0
    dropped: int = 0
    cost_usd: Decimal = Decimal("0")
    blocks: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """The stage produced a usable reply."""
        return not self.errors and self.calls > 0

    def as_dict(self) -> dict[str, object]:
        """JSON-friendly form for ``eval_runs.metrics``."""
        return {
            "stage": self.stage,
            "model": self.model,
            "calls": self.calls,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "dropped": self.dropped,
            "cost_usd": str(self.cost_usd),
            "blocks": self.blocks,
            "errors": list(self.errors),
        }


def _ensure_index(session: Session, audit_id: uuid.UUID, tools: RepositoryTools) -> bool:
    """Build the symbol/chunk/vector index the retrieval tools need, once.

    The audit pipeline indexes files for the scanners but stops there; symbols,
    chunks, call edges and vectors are separate stages. Retrieval without them
    returns nothing at all, which would look like an agent that found no problems.
    Building them here keeps the stage correct for any audit that has passed
    intake, and the work is skipped when the index already exists.
    """
    already = session.execute(
        select(Chunk.id).where(Chunk.audit_id == audit_id).limit(1)
    ).first()
    if already is not None:
        return True
    root = tools.snapshot_root
    if root is None or not Path(root).is_dir():
        return False

    index_snapshot(session, audit_id, root)
    index_audit(session, audit_id, snapshot_root=root)
    index_graph(session, audit_id, snapshot_root=root)
    embed_chunks(session, audit_id)
    return True


def _collect_context(tools: RepositoryTools) -> tuple[DataBlock, ...]:
    """Run the review questions through retrieval and keep a bounded, distinct set."""
    blocks: list[DataBlock] = []
    seen: set[tuple[str, int]] = set()
    total = 0
    for query in REVIEW_QUERIES:
        for block in tools.retrieve_context(query, k=6):
            key = (block.file_path, block.line_start)
            if key in seen:
                continue
            if total + len(block.body) > MAX_CONTEXT_CHARS or len(blocks) >= MAX_BLOCKS:
                return tuple(blocks)
            seen.add(key)
            blocks.append(block)
            total += len(block.body)
    return tuple(blocks)


def _file_ids(session: Session, audit_id: uuid.UUID) -> dict[str, uuid.UUID]:
    return {
        path: file_id
        for file_id, path in session.execute(
            select(File.id, File.path).where(File.audit_id == audit_id)
        ).all()
    }


def _store(
    session: Session,
    audit_id: uuid.UUID,
    findings: tuple[AgentFinding, ...],
    tools: RepositoryTools,
) -> int:
    """Insert the accepted findings; returns how many were actually new."""
    existing = existing_fingerprints(session, audit_id)
    file_ids = _file_ids(session, audit_id)
    stored = 0
    for finding in findings:
        fingerprint = finding_fingerprint(
            category=finding.category,
            rule_id=SOURCE_TOOL,
            file_path=finding.file,
            line_start=finding.line_start,
            line_end=finding.line_end,
        )
        if fingerprint in existing:
            continue

        lines = tools.file_lines(finding.file) or []
        quote = finding.evidence_quote or "\n".join(
            lines[finding.line_start - 1 : finding.line_end]
        )
        row = Finding(
            audit_id=audit_id,
            category=Category(finding.category),
            title=finding.title,
            severity=Severity(finding.severity),
            confidence=Decimal(str(round(finding.confidence, 2))),
            origin=FindingOrigin.AGENT,
            source_tool=SOURCE_TOOL,
            verification_status=VerificationStatus.AI_SUGGESTED,
            verdict=Verdict.ISSUE,
            reasoning=finding.reasoning,
            recommendation=finding.recommendation,
            fingerprint=fingerprint,
        )
        session.add(row)
        session.flush()

        session.add(
            Evidence(
                finding_id=row.id,
                file_id=file_ids.get(finding.file),
                file_path=finding.file,
                line_start=finding.line_start,
                line_end=finding.line_end,
                snippet=redact_text(quote)[:MAX_SNIPPET_CHARS],
                kind=EvidenceKind.PRIMARY,
            )
        )
        existing.add(fingerprint)
        stored += 1

    session.commit()
    return stored


def run_baseline_single(
    session: Session,
    audit_id: uuid.UUID,
    *,
    client: LLMClient | None = None,
    tools: RepositoryTools | None = None,
) -> AgentOutcome:
    """Review one audited repository with a single agent call."""
    repository_tools = tools or RepositoryTools(session, audit_id)
    if not _ensure_index(session, audit_id, repository_tools):
        logger.info("baseline agent has no readable snapshot for audit %s", audit_id)
        return AgentOutcome(errors=("no readable snapshot to index",))

    blocks = _collect_context(repository_tools)
    if not blocks:
        logger.info("baseline agent found no context for audit %s", audit_id)
        return AgentOutcome(errors=("no indexed context to review",))

    known = repository_tools.findings(limit=20)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_user_message(blocks, REVIEW_QUERIES, known),
        },
    ]

    model_client = client or LLMClient(session, audit_id)
    calls = 0
    rejected_total = 0
    cost = Decimal("0")
    model: str | None = None
    note: str | None = None

    for attempt in range(RETRIES + 1):
        try:
            result = model_client.complete(
                AGENT_ROLE,
                messages,
                # JSON with up to a few dozen findings needs headroom over the
                # 1024-token default, or a valid reply arrives truncated.
                max_tokens=MAX_REPLY_TOKENS,
            )
        except BudgetExceeded as exc:
            logger.warning("baseline agent stopped for audit %s: %s", audit_id, exc)
            return AgentOutcome(
                model=model,
                calls=calls,
                rejected=rejected_total,
                dropped=rejected_total,
                cost_usd=cost,
                blocks=len(blocks),
                errors=(f"budget exhausted: {exc}",),
            )
        except LLMError as exc:
            logger.warning("baseline agent could not call the model for %s: %s", audit_id, exc)
            return AgentOutcome(
                model=model,
                calls=calls,
                rejected=rejected_total,
                dropped=rejected_total,
                cost_usd=cost,
                blocks=len(blocks),
                errors=(str(exc),),
            )

        calls += 1
        model = result.model
        cost += result.cost_usd

        try:
            report = parse_report(result.text)
        except InvalidOutput as exc:
            note = f"Your previous reply could not be parsed: {exc}. Reply with the JSON object only."
            logger.info("baseline agent reply rejected for audit %s: %s", audit_id, exc)
            rejected_total += 1
            if attempt == RETRIES:
                return AgentOutcome(
                    model=model,
                    calls=calls,
                    rejected=rejected_total,
                    dropped=rejected_total,
                    cost_usd=cost,
                    blocks=len(blocks),
                    errors=(f"unparseable reply after {calls} call(s): {exc}",),
                )
            messages = [
                *messages,
                {"role": "assistant", "content": result.text},
                {"role": "user", "content": note},
            ]
            continue

        validation = validate_report(report, repository_tools)
        rejected_total += len(validation.rejected)

        # A partly valid reply is progress: keep what a reader could verify and do
        # not spend the one retry on it. A reply in which nothing survives is either
        # a hallucination or a schema the model cannot follow, which is exactly what
        # the single retry is for. An empty list is a legitimate "nothing found".
        if validation.accepted or not validation.rejected:
            stored = _store(session, audit_id, validation.accepted, repository_tools)
            logger.info(
                "baseline agent stored %d finding(s) for audit %s in %d call(s)",
                stored,
                audit_id,
                calls,
            )
            return AgentOutcome(
                model=model,
                calls=calls,
                accepted=stored,
                rejected=rejected_total,
                cost_usd=cost,
                blocks=len(blocks),
            )

        logger.info(
            "baseline agent findings rejected for audit %s: %d",
            audit_id,
            len(validation.rejected),
        )
        if attempt == RETRIES:
            return AgentOutcome(
                model=model,
                calls=calls,
                rejected=rejected_total,
                dropped=len(validation.rejected),
                cost_usd=cost,
                blocks=len(blocks),
                errors=(
                    f"{len(validation.rejected)} unverifiable finding(s) after {calls} call(s)",
                ),
            )
        messages = [
            *messages,
            {"role": "assistant", "content": result.text},
            {"role": "user", "content": describe_errors(validation.rejected)},
        ]

    # Unreachable: every branch above returns on the final attempt.
    return AgentOutcome(  # pragma: no cover
        model=model,
        calls=calls,
        rejected=rejected_total,
        dropped=rejected_total,
        cost_usd=cost,
        blocks=len(blocks),
        errors=("agent produced no usable reply",),
    )
