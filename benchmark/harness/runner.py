"""Run one configuration over the fixture repositories.

Each repository is imported into a throwaway ``repositories``/``audits`` pair, the
configuration's stages run through the real pipeline (``app.jobs.audit.run_audit``
plus, for the agent configurations, ``app.agents.AGENT_STAGE``), and the stored
findings are scored against the manifest.

The fixture is read as data by the indexer and the scanners; it is never executed
or imported.

Two deliberate choices:

* The audit and its repository are deleted afterwards unless ``keep_audits`` is
  set. The durable record of a run is ``eval_runs`` plus one ``eval_results`` row
  per expected issue; keeping the audits too would grow the database on every
  invocation and make findings from different runs hard to tell apart.
* ``eval_results.cost_usd`` and ``latency_ms`` are left NULL. Cost and latency
  belong to the run of a repository, not to an individual expected issue, and
  copying them onto each row would triple-count in any naive sum. They are stored
  per repository in ``eval_runs.metrics``.
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

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session, selectinload, sessionmaker  # noqa: E402

from app.db.enums import AuditStatus, SourceType  # noqa: E402
from app.db.models import (  # noqa: E402
    AgentRun,
    Audit,
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


@dataclass(frozen=True)
class IssueOutcome:
    """What happened to one expected issue."""

    issue_id: str
    repo: str
    detected: bool
    evidence_correct: bool | None = None
    severity_correct: bool | None = None


@dataclass(frozen=True)
class RepoEvaluation:
    """One repository's score plus the per-issue rows that go into ``eval_results``."""

    score: metrics.RepoScore
    outcomes: tuple[IssueOutcome, ...]
    scanner_status: dict[str, str] = field(default_factory=dict)
    agent_outcome: dict[str, object] | None = None


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


def _load_findings(session: Session, audit_id: uuid.UUID) -> tuple[ReportedFinding, ...]:
    """Read the stored findings and put them in the harness's canonical order."""
    rows = session.execute(
        select(Finding)
        .where(Finding.audit_id == audit_id)
        .options(selectinload(Finding.evidence))
        .order_by(Finding.category, Finding.id)
    ).scalars()

    findings: list[ReportedFinding] = []
    for finding in rows:
        spans = tuple(
            Span(file_path=item.file_path, line_start=item.line_start, line_end=item.line_end)
            for item in finding.evidence
        )
        findings.append(
            ReportedFinding(
                category=finding.category.value,
                severity=finding.severity.value,
                evidence=spans,
                title=finding.title,
            )
        )
    return matching.order_findings(tuple(findings))


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
    findings: tuple[ReportedFinding, ...],
    snapshot: Path,
    cost_usd: Decimal,
    runtime_ms: int,
) -> tuple[metrics.RepoScore, tuple[IssueOutcome, ...]]:
    """Match, verify and count one repository."""
    outcome = matching.match_all(issues, findings)

    outcomes: list[IssueOutcome] = []
    evidence_correct = severity_correct = cross_file_matched = 0
    matched_ids: list[str] = []

    for assignment in outcome.assignments:
        issue = assignment.issue
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
            )
        )

    for missed in outcome.missed:
        outcomes.append(IssueOutcome(issue_id=missed.id, repo=repo, detected=False))

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
    return score, tuple(outcomes)


def evaluate_repo(
    configuration: configs.Configuration,
    repo: str,
    issues: tuple[ExpectedIssue, ...],
    snapshot: Path,
    *,
    session_factory: sessionmaker[Session] | None = None,
    keep_audits: bool = False,
) -> RepoEvaluation:
    """Run one configuration over one fixture and score it."""
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
        score, outcomes = _score(
            repo=repo,
            issues=issues,
            findings=findings,
            snapshot=Path(snapshot),
            cost_usd=_spent_usd(session, audit.id),
            runtime_ms=runtime_ms,
        )
        return RepoEvaluation(
            score=score,
            outcomes=outcomes,
            scanner_status=_scanner_status(session, audit.id),
            agent_outcome=agent_outcome,
        )
    finally:
        if repository is not None and not keep_audits:
            if audit is not None:
                session.delete(audit)  # cascades findings, evidence, scanner_runs, agent_runs
                session.commit()
            session.delete(repository)
            session.commit()
        session.close()


def run_configuration(
    configuration: configs.Configuration,
    manifest: Manifest,
    repos: tuple[str, ...] | None = None,
    *,
    repos_root: Path | None = None,
    session_factory: sessionmaker[Session] | None = None,
    keep_audits: bool = False,
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
            keep_audits=keep_audits,
        )
        for repo in selected
    )
    total = metrics.totals(tuple(evaluation.score for evaluation in evaluations))

    if not persist:
        return RunRecord(
            eval_run_id=None,
            configuration=configuration,
            benchmark_version=manifest.version,
            evaluations=evaluations,
            total=total,
            started_at=started_at,
        )

    factory = session_factory or get_session_factory()
    session = factory()
    try:
        eval_run = EvalRun(
            config=configuration.config,
            benchmark_version=manifest.version,
            started_at=started_at,
            metrics={
                "config": configuration.name,
                "summary": configuration.summary,
                "stages": list(configuration.stages),
                "repo_count": len(selected),
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
            },
        )
        session.add(eval_run)
        session.flush()

        for evaluation in evaluations:
            for outcome in evaluation.outcomes:
                session.add(
                    EvalResult(
                        eval_run_id=eval_run.id,
                        repo_name=outcome.repo,
                        expected_issue_id=outcome.issue_id,
                        detected=outcome.detected,
                        evidence_correct=outcome.evidence_correct,
                        severity_correct=outcome.severity_correct,
                        cost_usd=None,
                        latency_ms=None,
                    )
                )
        session.commit()
        session.refresh(eval_run)
        return RunRecord(
            eval_run_id=eval_run.id,
            configuration=configuration,
            benchmark_version=manifest.version,
            evaluations=evaluations,
            total=total,
            started_at=started_at,
        )
    finally:
        session.close()
