"""Run one configuration over the fixture repositories.

Each repository is imported into a throwaway ``repositories``/``audits`` pair, the
configuration's stages run through the real pipeline (``app.jobs.audit.run_audit``
plus, for the agent configurations, ``app.agents.AGENT_STAGE``), and the stored
findings are scored against the manifest.

The fixture is read as data by the indexer and the scanners; it is never executed
or imported.

Two deliberate choices:

* A persisted run keeps its audit and repository, because ``eval_results`` names
  the finding that satisfied each issue and deleting the audit would cascade to
  those findings and blank the references. Pruning is the caller's job and happens
  only when the run is not persisted, or when ``prune_after`` is passed; the run's
  ``metrics`` then records ``pruned: true`` so a NULL id is never read as a bug.
* ``eval_results.cost_usd`` and ``latency_ms`` are the repository's own cost and
  wall time, repeated on each of its rows because step 2.6b reads them there.
  They describe the run, not the issue, so never sum them down a repository.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from benchmark.harness import bootstrap

bootstrap.add_backend_to_path()

from sqlalchemy import delete, func, select  # noqa: E402
from sqlalchemy.orm import Session, selectinload, sessionmaker  # noqa: E402

from app.db.enums import AuditStatus, SourceType  # noqa: E402
from app.db.models import (  # noqa: E402
    AgentRun,
    Audit,
    EvalExtras,
    EvalResult,
    EvalRun,
    Finding,
    Repository,
    ScannerRun,
)
from app.db.session import get_session_factory  # noqa: E402
from app.jobs.audit import run_audit  # noqa: E402
from benchmark.harness import configs, evidence, matching, metrics  # noqa: E402
from benchmark.harness.expected import ExpectedIssue, Manifest  # noqa: E402
from benchmark.harness.matching import ReportedFinding, Span  # noqa: E402


# Recorded when the matcher found no remaining finding with the issue's category,
# file and an overlapping line. It is the matcher's own reason, not a placeholder.
MISS_NO_MATCH = "no_matching_finding"


@dataclass(frozen=True)
class IssueOutcome:
    """What happened to one expected issue, and which finding decided it."""

    issue_id: str
    repo: str
    detected: bool
    evidence_correct: bool | None = None
    severity_correct: bool | None = None
    # The finding that satisfied the issue, or the reason it did not. A missed
    # issue never names a finding: nothing was assigned to it.
    matched_finding_id: uuid.UUID | None = None
    matched_origin: str | None = None
    matched_source_tool: str | None = None
    matched_file: str | None = None
    matched_line_start: int | None = None
    matched_line_end: int | None = None
    is_cross_file: bool | None = None
    miss_reason: str | None = None


@dataclass(frozen=True)
class ExtraFinding:
    """A stored finding that matched no expected issue."""

    finding_id: uuid.UUID | None
    repo: str
    category: str | None
    title: str | None
    file: str | None
    line_start: int | None
    line_end: int | None
    origin: str | None
    source_tool: str | None
    # Left NULL until a pipeline stage (step 2.10 validation) produces one.
    rejection_reason: str | None = None


@dataclass(frozen=True)
class LoadedFinding:
    """One stored finding, with both its row and the reduced form matching reads."""

    row: Finding
    reported: ReportedFinding


@dataclass(frozen=True)
class RepoEvaluation:
    """One repository's score plus the rows that go into ``eval_results``/``eval_extras``."""

    score: metrics.RepoScore
    outcomes: tuple[IssueOutcome, ...]
    extras: tuple[ExtraFinding, ...] = ()
    scanner_status: dict[str, str] = field(default_factory=dict)
    agent_models: tuple[str, ...] = ()
    agent_outcome: dict[str, object] | None = None
    # The throwaway audit and repository, kept only so the caller can prune them
    # once the run is written. Nothing is deleted here: the findings they hold are
    # what the persisted rows point at.
    audit_id: uuid.UUID | None = None
    repository_id: uuid.UUID | None = None


@dataclass(frozen=True)
class RunRecord:
    """The result of one invocation of the harness.

    ``eval_run_id`` is ``None`` when the run was not persisted.
    """

    eval_run_id: uuid.UUID | None
    configuration: configs.Configuration
    benchmark_version: str
    evaluations: tuple[RepoEvaluation, ...]
    total: metrics.RepoScore
    started_at: datetime

    @property
    def scores(self) -> tuple[metrics.RepoScore, ...]:
        """Per-repository scores, in manifest order."""
        return tuple(evaluation.score for evaluation in self.evaluations)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# A model-driven configuration gets a budget on its throwaway audit, so a
# misbehaving agent stops the run instead of spending without a ceiling. Scanner
# configurations never call a model, so they are left unlimited.
DEFAULT_AGENT_BUDGET_USD = Decimal("0.25")

# The matching rubric this harness implements. Bump it whenever the rule in
# matching.py changes, so two runs are never compared as if they were measured
# the same way. The manifest has its own version, which describes the fixture
# set and is recorded separately as ``manifest_version``.
BENCHMARK_VERSION = "2"


def _fixture_name(configuration: configs.Configuration, repo: str) -> str:
    """A repository name unique per invocation, so repeated runs never collide."""
    return f"eval-{configuration.name}-{repo}-{uuid.uuid4().hex[:8]}"


def _import_fixture(
    session: Session, configuration: configs.Configuration, repo: str, snapshot: Path
) -> tuple[Repository, Audit]:
    """Create the repository and audit rows the pipeline expects."""
    repository = Repository(
        name=_fixture_name(configuration, repo),
        source_type=SourceType.ZIP,
        source_ref=repo,
        storage_path=str(snapshot),
    )
    session.add(repository)
    session.flush()

    audit = Audit(
        repository_id=repository.id,
        status=AuditStatus.QUEUED,
        config={"eval_config": configuration.name, "fixture_repo": repo},
        budget_usd=DEFAULT_AGENT_BUDGET_USD if configuration.uses_agent_stage else None,
    )
    session.add(audit)
    session.commit()
    session.refresh(repository)
    session.refresh(audit)
    return repository, audit


def _load_findings(session: Session, audit_id: uuid.UUID) -> tuple[LoadedFinding, ...]:
    """Read the stored findings and pair each with the form the rule reads.

    The pairing is kept because the matching rule works on ``ReportedFinding``
    while the instrumentation needs the row it came from (id, origin, tool).
    ``order_findings`` returns the same objects, so identity maps back exactly.
    """
    rows = session.execute(
        select(Finding)
        .where(Finding.audit_id == audit_id)
        .options(selectinload(Finding.evidence))
        .order_by(Finding.category, Finding.id)
    ).scalars()

    loaded: list[LoadedFinding] = []
    for finding in rows:
        spans = tuple(
            Span(file_path=item.file_path, line_start=item.line_start, line_end=item.line_end)
            for item in finding.evidence
        )
        loaded.append(
            LoadedFinding(
                row=finding,
                reported=ReportedFinding(
                    category=finding.category.value,
                    severity=finding.severity.value,
                    evidence=spans,
                    title=finding.title,
                    reasoning=finding.reasoning or "",
                ),
            )
        )

    ordered = matching.order_findings(tuple(item.reported for item in loaded))
    by_reported = {id(item.reported): item for item in loaded}
    return tuple(by_reported[id(reported)] for reported in ordered)


def _matching_span(issue: ExpectedIssue, finding: ReportedFinding) -> Span | None:
    """The cited span that actually satisfied the issue, in the rule's own terms."""
    for span in finding.evidence:
        if matching.same_file(issue.file, span.file_path) and matching.overlaps(
            issue.line_start, issue.line_end, span.line_start, span.line_end
        ):
            return span
    return finding.evidence[0] if finding.evidence else None


def _extra_finding(repo: str, finding: Finding) -> ExtraFinding:
    """A finding that matched nothing, kept with the location it really cites."""
    first = finding.evidence[0] if finding.evidence else None
    return ExtraFinding(
        finding_id=finding.id,
        repo=repo,
        category=finding.category.value,
        title=finding.title,
        file=first.file_path if first else None,
        line_start=first.line_start if first else None,
        line_end=first.line_end if first else None,
        origin=finding.origin.value,
        source_tool=finding.source_tool,
        rejection_reason=None,
    )


def _spent_usd(session: Session, audit_id: uuid.UUID) -> Decimal:
    """Model spend for this audit, straight from the per-call ledger."""
    total = session.execute(
        select(func.coalesce(func.sum(AgentRun.cost_usd), 0)).where(AgentRun.audit_id == audit_id)
    ).scalar_one()
    return Decimal(total or 0)


def _scanner_status(session: Session, audit_id: uuid.UUID) -> dict[str, str]:
    """Each scanner's terminal status, so a failed tool cannot look like a miss."""
    rows = session.execute(
        select(ScannerRun.tool, ScannerRun.status).where(ScannerRun.audit_id == audit_id)
    ).all()
    return {tool.value: status.value for tool, status in rows}


def _score(
    repo: str,
    issues: tuple[ExpectedIssue, ...],
    findings: tuple[LoadedFinding, ...],
    snapshot: Path,
    cost_usd: Decimal,
    runtime_ms: int,
) -> tuple[metrics.RepoScore, tuple[IssueOutcome, ...], tuple[ExtraFinding, ...]]:
    """Match, verify and count one repository, recording which finding decided each issue."""
    reported = tuple(item.reported for item in findings)
    by_reported = {id(item.reported): item for item in findings}
    outcome = matching.match_all(issues, reported)

    outcomes: list[IssueOutcome] = []
    evidence_correct = severity_correct = cross_file_matched = 0
    matched_ids: list[str] = []

    for assignment in outcome.assignments:
        issue = assignment.issue
        row = by_reported[id(assignment.finding)].row
        span = _matching_span(issue, assignment.finding)
        cited_right = evidence.evidence_is_correct(issue, assignment.finding, snapshot)
        severity_right = issue.severity.lower() == assignment.finding.severity.lower()
        evidence_correct += int(cited_right)
        severity_correct += int(severity_right)
        cross_file_matched += int(issue.cross_file)
        matched_ids.append(issue.id)
        outcomes.append(
            IssueOutcome(
                issue_id=issue.id,
                repo=repo,
                detected=True,
                evidence_correct=cited_right,
                severity_correct=severity_right,
                matched_finding_id=row.id,
                matched_origin=row.origin.value,
                matched_source_tool=row.source_tool,
                matched_file=span.file_path if span else None,
                matched_line_start=span.line_start if span else None,
                matched_line_end=span.line_end if span else None,
                is_cross_file=issue.cross_file,
                miss_reason=None,
            )
        )

    for missed in outcome.missed:
        outcomes.append(
            IssueOutcome(
                issue_id=missed.id,
                repo=repo,
                detected=False,
                is_cross_file=missed.cross_file,
                miss_reason=MISS_NO_MATCH,
            )
        )

    extras = tuple(
        _extra_finding(repo, by_reported[id(item)].row) for item in outcome.false_positives
    )

    score = metrics.RepoScore(
        repo=repo,
        expected=len(issues),
        findings=len(findings),
        matched=len(outcome.assignments),
        duplicates=len(outcome.duplicates),
        evidence_correct=evidence_correct,
        severity_correct=severity_correct,
        cross_file_expected=sum(1 for issue in issues if issue.cross_file),
        cross_file_matched=cross_file_matched,
        cost_usd=cost_usd,
        runtime_ms=runtime_ms,
        matched_ids=tuple(matched_ids),
        missed_ids=tuple(issue.id for issue in outcome.missed),
        false_positives=tuple(item.describe() for item in outcome.false_positives),
    )
    return score, tuple(outcomes), extras


def evaluate_repo(
    configuration: configs.Configuration,
    repo: str,
    issues: tuple[ExpectedIssue, ...],
    snapshot: Path,
    *,
    session_factory: sessionmaker[Session] | None = None,
) -> RepoEvaluation:
    """Run one configuration over one fixture and score it.

    Nothing is deleted: the caller owns the throwaway audit and repository and
    prunes them when it is done with the findings they hold.
    """
    available, reason = configs.availability(configuration)
    if not available:
        raise configs.ConfigurationUnavailable(reason or "configuration unavailable")

    factory = session_factory or get_session_factory()
    session = factory()
    repository = audit = None
    try:
        repository, audit = _import_fixture(session, configuration, repo, Path(snapshot))

        started = time.monotonic()
        run_audit(audit.id, session_factory=factory)
        agent_outcome: dict[str, object] | None = None
        if configuration.uses_agent_stage:
            stage = configs.agent_stages().get(configuration.config)
            if stage is None:  # pragma: no cover - availability() already refused
                raise configs.ConfigurationUnavailable(f"{configuration.name} needs an agent stage")
            produced = stage(session, audit.id)
            describe = getattr(produced, "as_dict", None)
            agent_outcome = describe() if callable(describe) else None
        runtime_ms = int((time.monotonic() - started) * 1000)

        findings = _load_findings(session, audit.id)
        score, outcomes, extras = _score(
            repo=repo,
            issues=issues,
            findings=findings,
            snapshot=Path(snapshot),
            cost_usd=_spent_usd(session, audit.id),
            runtime_ms=runtime_ms,
        )
        # Read before the audit is pruned; the models are part of the result.
        agent_models = tuple(
            sorted(
                {
                    model
                    for model in session.execute(
                        select(AgentRun.model).where(AgentRun.audit_id == audit.id)
                    ).scalars()
                    if model
                }
            )
        )
        return RepoEvaluation(
            score=score,
            outcomes=outcomes,
            extras=extras,
            scanner_status=_scanner_status(session, audit.id),
            agent_models=agent_models,
            agent_outcome=agent_outcome,
            audit_id=audit.id,
            repository_id=repository.id,
        )
    finally:
        session.close()


def _persist_run(
    session: Session,
    *,
    configuration: configs.Configuration,
    benchmark_version: str,
    manifest_version: str,
    evaluations: tuple[RepoEvaluation, ...],
    total: metrics.RepoScore,
    started_at: datetime,
    pruned: bool = False,
) -> EvalRun:
    """Write one run with its per-issue and per-extra rows.

    Every value written here comes from the pipeline: the issue outcomes carry the
    finding they matched or the reason they missed, and the extra findings carry
    the location they cite. Anything the pipeline could not produce stays NULL.
    """
    eval_run = EvalRun(
        config=configuration.config,
        benchmark_version=benchmark_version,
        started_at=started_at,
        metrics={
            "config": configuration.name,
            "manifest_version": manifest_version,
            # Only set when the caller asked for pruning, so a NULL finding id is
            # explained rather than mistaken for a lost measurement.
            **({"pruned": True} if pruned else {}),
            "summary": configuration.summary,
            "stages": list(configuration.stages),
            "repo_count": len(evaluations),
            "totals": total.to_json(),
            "repos": [evaluation.score.to_json() for evaluation in evaluations],
            "scanner_status": {
                evaluation.score.repo: evaluation.scanner_status for evaluation in evaluations
            },
            "agents": {
                evaluation.score.repo: evaluation.agent_outcome
                for evaluation in evaluations
                if evaluation.agent_outcome is not None
            },
            # Which model actually answered, so two runs are comparable.
            "models": {
                evaluation.score.repo: list(evaluation.agent_models)
                for evaluation in evaluations
                if evaluation.agent_models
            },
        },
    )
    session.add(eval_run)
    session.flush()

    for evaluation in evaluations:
        # Cost and latency belong to the repository; they are repeated per row.
        for outcome in evaluation.outcomes:
            session.add(
                EvalResult(
                    eval_run_id=eval_run.id,
                    repo_name=outcome.repo,
                    expected_issue_id=outcome.issue_id,
                    detected=outcome.detected,
                    evidence_correct=outcome.evidence_correct,
                    severity_correct=outcome.severity_correct,
                    cost_usd=evaluation.score.cost_usd,
                    latency_ms=evaluation.score.runtime_ms,
                    matched_finding_id=outcome.matched_finding_id,
                    matched_origin=outcome.matched_origin,
                    matched_source_tool=outcome.matched_source_tool,
                    matched_file=outcome.matched_file,
                    matched_line_start=outcome.matched_line_start,
                    matched_line_end=outcome.matched_line_end,
                    is_cross_file=outcome.is_cross_file,
                    miss_reason=outcome.miss_reason,
                )
            )
        for extra in evaluation.extras:
            session.add(
                EvalExtras(
                    eval_run_id=eval_run.id,
                    finding_id=extra.finding_id,
                    repo=extra.repo,
                    category=extra.category,
                    title=extra.title,
                    file=extra.file,
                    line_start=extra.line_start,
                    line_end=extra.line_end,
                    origin=extra.origin,
                    source_tool=extra.source_tool,
                    rejection_reason=extra.rejection_reason,
                )
            )

    session.commit()
    session.refresh(eval_run)
    return eval_run


def _prune_audits(session: Session, evaluations: tuple[RepoEvaluation, ...]) -> None:
    """Delete the throwaway audits and repositories once the run is written.

    Cascade rules take the findings with them, and the foreign keys then set
    ``matched_finding_id``/``finding_id`` to NULL. That is why pruning is opt-in
    for a persisted run and recorded in ``metrics`` when it happens.
    """
    audit_ids = [item.audit_id for item in evaluations if item.audit_id is not None]
    repository_ids = [
        item.repository_id for item in evaluations if item.repository_id is not None
    ]
    if audit_ids:
        session.execute(delete(Audit).where(Audit.id.in_(audit_ids)))
    if repository_ids:
        session.execute(delete(Repository).where(Repository.id.in_(repository_ids)))
    session.commit()


def run_configuration(
    configuration: configs.Configuration,
    manifest: Manifest,
    repos: tuple[str, ...] | None = None,
    *,
    repos_root: Path | None = None,
    session_factory: sessionmaker[Session] | None = None,
    keep_audits: bool = False,
    prune_after: bool = False,
    persist: bool = True,
) -> RunRecord:
    """Evaluate one configuration over every selected repository.

    With ``persist=False`` the audits are still run and scored, but nothing is
    written to ``eval_runs``/``eval_results`` — useful while editing a fixture.
    """
    available, reason = configs.availability(configuration)
    if not available:
        raise configs.ConfigurationUnavailable(reason or "configuration unavailable")

    root = Path(repos_root) if repos_root is not None else Path("benchmark") / "repos"
    selected = repos or manifest.repos
    started_at = _now()

    evaluations = tuple(
        evaluate_repo(
            configuration,
            repo,
            manifest.for_repo(repo),
            root / repo,
            session_factory=session_factory,
        )
        for repo in selected
    )
    total = metrics.totals(tuple(evaluation.score for evaluation in evaluations))

    # A persisted run keeps its audits unless asked otherwise, so the ids it just
    # wrote still resolve. An unpersisted run has nothing to protect and is pruned.
    should_prune = not keep_audits and (prune_after or not persist)

    factory = session_factory or get_session_factory()
    session = factory()
    eval_run_id: uuid.UUID | None = None
    try:
        if persist:
            try:
                eval_run_id = _persist_run(
                    session,
                    configuration=configuration,
                    benchmark_version=BENCHMARK_VERSION,
                    manifest_version=manifest.version,
                    evaluations=evaluations,
                    total=total,
                    started_at=started_at,
                    pruned=should_prune,
                ).id
            except Exception:
                # _persist_run commits once, so a failure leaves no partial run.
                session.rollback()
                raise
    finally:
        if should_prune:
            _prune_audits(session, evaluations)
        session.close()

    return RunRecord(
        eval_run_id=eval_run_id,
        configuration=configuration,
        benchmark_version=BENCHMARK_VERSION,
        evaluations=evaluations,
        total=total,
        started_at=started_at,
    )
