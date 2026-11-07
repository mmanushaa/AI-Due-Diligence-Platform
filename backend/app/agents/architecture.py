"""The architecture agent, as a CrewAI crew (step 2.9).

The security crew reads code to find what one file cannot show. This one starts
from what the index already knows about the *shape* of the repository and asks a
model to judge it:

    module graph   which file imports which, straight from ``module_edges``
    cycles         the import cycles step 2.2 detects, with the import line that
                   closes each one
    entry points   modules nothing imports, which is where a reader starts
    large modules  the biggest files by line count, measured against the median
    retrieved text what the model reads before it calls any of it a problem

The facts are computed, not guessed: a cycle is a cycle because Tarjan found it,
and the citation is an import statement this module read out of the file. The model
decides which of them matter, how serious they are and how to explain them, which
is the part a graph walk cannot do.

Every path a finding cites is checked twice: the schema's line range has to fall
inside the file, and the file has to be one this audit indexed. A finding that cites
a module the repository does not have - the usual failure when a model reasons from
memory about "typical" layouts - is rejected and counted, not stored.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from statistics import median
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.baseline_single import AgentOutcome
from app.agents.crew import GatewayLLM, crew_llm, read_only_tools
from app.agents.prompts import fill, load_prompt
from app.agents.schema import (
    AgentFinding,
    AgentReport,
    InvalidOutput,
    RejectedFinding,
    ValidationResult,
    parse_report,
    validate_report,
)
from app.agents.store import ensure_index, store_findings
from app.agents.tools import RepositoryTools
from app.db.enums import AgentName
from app.db.models import File, Symbol
from app.indexing.graph import ModuleResolver, find_cycles
from app.llm.client import LLMClient, LLMError

logger = logging.getLogger(__name__)

SOURCE_TOOL = "architecture_agent"
AGENT_ROLE = AgentName.ARCHITECTURE
MAX_ITERATIONS = 8
#: A module has to be this many lines, *and* this many times the median, before it
#: is worth calling large. Both, because a repository of small files has no large
#: module and a repository of large files has no outlier.
LARGE_MODULE_LINES = 300
LARGE_MODULE_RATIO = 3.0
MAX_LARGE_MODULES = 5
#: Directory name to layer, lowest first. A module importing from a higher layer
#: than its own is the layering violation a reviewer can actually act on.
LAYERS: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "storage",
        frozenset(
            {"db", "database", "models", "model", "store", "storage", "repositories", "repository"}
        ),
    ),
    ("domain", frozenset({"services", "service", "domain", "core", "lib", "utils", "helpers"})),
    (
        "interface",
        frozenset({"routes", "route", "api", "controllers", "handlers", "views", "web", "endpoints"}),
    ),
)


@dataclass(frozen=True)
class ImportEdge:
    """One import statement that points at a file inside the repository."""

    from_path: str
    line: int
    to_path: str
    statement: str

    def render(self) -> str:
        """How the prompt shows it: the citation, then what it points at."""
        return f"{self.from_path}:{self.line} imports {self.to_path} -- {self.statement}"


@dataclass(frozen=True)
class ModuleCycle:
    """A cycle, as the ordered edges that form it."""

    edges: tuple[ImportEdge, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        """The files in the cycle, in the order the walk visited them."""
        return tuple(edge.from_path for edge in self.edges)

    def render(self) -> str:
        """One line per edge, so the model can cite any of them."""
        return "\n".join(f"  - {edge.render()}" for edge in self.edges)


@dataclass(frozen=True)
class LargeModule:
    """A module big enough to be worth a second look."""

    path: str
    lines: int
    symbols: int
    ratio: float

    def render(self) -> str:
        return f"{self.path}: {self.lines} lines, {self.symbols} symbols, {self.ratio:.1f}x median"


@dataclass(frozen=True)
class LayerEdge:
    """An import that points from a lower layer to a higher one."""

    edge: ImportEdge
    from_layer: str
    to_layer: str

    def render(self) -> str:
        return f"{self.edge.render()} ({self.from_layer} importing the {self.to_layer} layer)"


@dataclass(frozen=True)
class ArchitectureFacts:
    """Everything the architecture agent is told before it reads any code."""

    modules: int = 0
    imports: int = 0
    cycles: tuple[ModuleCycle, ...] = ()
    entry_points: tuple[str, ...] = ()
    large_modules: tuple[LargeModule, ...] = ()
    layer_edges: tuple[LayerEdge, ...] = ()

    def render(self) -> str:
        """The facts as prompt text: numbers, then one block per signal."""
        blocks = [f"Modules: {self.modules}\nInternal imports: {self.imports}"]
        blocks.append(
            "Circular imports (each edge is a citable import statement):\n"
            + ("\n".join(cycle.render() for cycle in self.cycles) or "  none")
        )
        blocks.append(
            "Entry points (modules nothing imports):\n"
            + ("\n".join(f"  - {path}" for path in self.entry_points) or "  none")
        )
        blocks.append(
            "Large modules (over the line floor and the median ratio):\n"
            + ("\n".join(f"  - {module.render()}" for module in self.large_modules) or "  none")
        )
        blocks.append(
            "Imports that point against the layering:\n"
            + ("\n".join(f"  - {layer.render()}" for layer in self.layer_edges) or "  none")
        )
        return "\n\n".join(blocks)


def _layer_of(path: str) -> str | None:
    """The layer a module belongs to, by its directory names and its own name.

    Both, because a layer is a directory in one repository (``src/db/index.js``) and a
    file in another (``app/database.py``), and a rule that only knew directories would
    call the second one layerless and miss the violation entirely.
    """
    pure = PurePosixPath(path)
    parts = {part.lower() for part in pure.parts[:-1]} | {pure.stem.lower()}
    for name, directory_names in LAYERS:
        if parts & directory_names:
            return name
    return None


def _rank(layer: str) -> int:
    for index, (name, _) in enumerate(LAYERS):
        if name == layer:
            return index
    return -1


def _line_text(tools: RepositoryTools, path: str, line: int) -> str:
    """The import statement's own text, or an empty string if it cannot be read."""
    lines = tools.file_lines(path)
    if not lines or line < 1 or line > len(lines):
        return ""
    return lines[line - 1].strip()


def read_facts(
    session: Session, audit_id: uuid.UUID, tools: RepositoryTools
) -> ArchitectureFacts:
    """Build the facts from the stored graph and the snapshot, without a model.

    Import edges are resolved the way step 2.2 resolved them (``ModuleResolver`` over
    the parsed import symbols), so a cycle reported here is the cycle the graph holds
    and the line number is the import statement's own line rather than a guess at the
    top of the file.
    """
    paths = sorted(
        session.execute(select(File.path).where(File.audit_id == audit_id)).scalars()
    )
    if not paths:
        return ArchitectureFacts()

    resolver = ModuleResolver(paths)
    imports = session.execute(
        select(File.path, Symbol.line_start, Symbol.symbol_metadata)
        .join(Symbol, Symbol.file_id == File.id)
        .where(Symbol.audit_id == audit_id, Symbol.kind == "import")
        .order_by(File.path, Symbol.line_start)
    ).all()

    edges: list[ImportEdge] = []
    for path, line, metadata in imports:
        metadata = metadata or {}
        targets: list[str] = []
        if not metadata.get("module") and metadata.get("modules"):
            for single in metadata["modules"]:
                target = resolver.resolve(path, single)
                if target:
                    targets.append(target)
        else:
            target = resolver.resolve(
                path, metadata.get("module") or "", int(metadata.get("level") or 0)
            )
            if target:
                targets.append(target)
        for target in targets:
            if target == path:
                continue
            edges.append(
                ImportEdge(
                    from_path=path, line=int(line), to_path=target, statement=_line_text(tools, path, line)
                )
            )

    by_pair: dict[tuple[str, str], ImportEdge] = {}
    for edge in edges:
        by_pair.setdefault((edge.from_path, edge.to_path), edge)

    cycles: list[ModuleCycle] = []
    for cycle in find_cycles(paths, by_pair.keys()):
        cycle_edges = [
            by_pair[(path, cycle[(index + 1) % len(cycle)])]
            for index, path in enumerate(cycle)
            if (path, cycle[(index + 1) % len(cycle)]) in by_pair
        ]
        if cycle_edges:
            cycles.append(ModuleCycle(edges=tuple(cycle_edges)))

    imported = {edge.to_path for edge in edges}
    importing = {edge.from_path for edge in edges}
    entry_points = tuple(path for path in paths if path in importing and path not in imported)

    sizes = {path: len(tools.file_lines(path) or []) for path in paths}
    symbols_by_file = {
        path: int(count)
        for path, count in session.execute(
            select(File.path, func.count())
            .join(Symbol, Symbol.file_id == File.id)
            .where(Symbol.audit_id == audit_id)
            .group_by(File.path)
        ).all()
    }

    middle = median(sizes.values()) if sizes else 0
    large: list[LargeModule] = []
    if middle > 0:
        for path, size in sorted(sizes.items(), key=lambda item: (-item[1], item[0])):
            ratio = size / middle
            if size < LARGE_MODULE_LINES or ratio < LARGE_MODULE_RATIO:
                continue
            large.append(
                LargeModule(path=path, lines=size, symbols=symbols_by_file.get(path, 0), ratio=ratio)
            )
            if len(large) >= MAX_LARGE_MODULES:
                break

    layer_edges: list[LayerEdge] = []
    for edge in edges:
        from_layer, to_layer = _layer_of(edge.from_path), _layer_of(edge.to_path)
        if from_layer is None or to_layer is None or from_layer == to_layer:
            continue
        if _rank(from_layer) < _rank(to_layer):
            layer_edges.append(LayerEdge(edge=edge, from_layer=from_layer, to_layer=to_layer))

    return ArchitectureFacts(
        modules=len(paths),
        imports=len(edges),
        cycles=tuple(cycles),
        entry_points=entry_points,
        large_modules=tuple(large),
        layer_edges=tuple(layer_edges),
    )


def validate_architecture_report(report: AgentReport, tools: RepositoryTools) -> ValidationResult:
    """Only architecture findings, and only at files this repository has.

    ``validate_report`` already refuses a path the audit does not know, a line past
    the end of the file and a quote that is not at the cited lines. This adds the one
    rule the stage owns: a report that mixes in security or licence findings is
    answering a question nobody asked it, and those are dropped rather than stored
    under this stage's name.
    """
    checked = validate_report(report, tools)
    kept = tuple(finding for finding in checked.accepted if finding.category == "architecture")
    misplaced = tuple(
        RejectedFinding(finding.title, f"category {finding.category!r} is not architecture")
        for finding in checked.accepted
        if finding.category != "architecture"
    )
    return ValidationResult(accepted=kept, rejected=checked.rejected + misplaced)


def cycle_findings(facts: ArchitectureFacts) -> tuple[AgentFinding, ...]:
    """The cycles the graph proved, written without the model.

    A cycle is not an opinion: ``find_cycles`` found it and this module read the
    import statement back out of the file. Leaving that to a model means a run whose
    prose went sideways reports no cycle at all, which is the one failure the facts
    can prevent. The model's own finding, when it writes one, wins: this only fills
    in a cycle nothing in the accepted answer already names.
    """
    findings: list[AgentFinding] = []
    for cycle in facts.cycles:
        first = cycle.edges[0]
        rest = " -> ".join(f"{edge.from_path}:{edge.line}" for edge in cycle.edges)
        findings.append(
            AgentFinding(
                category="architecture",
                title=f"{cycle.edges[0].from_path} and {cycle.edges[0].to_path} import each other",
                file=first.from_path,
                line_start=first.line,
                line_end=first.line,
                severity="low",
                confidence=0.9,
                reasoning=(
                    f"These modules form an import cycle: {rest}. A cycle like this "
                    "means neither module can be read, tested or extracted on its own, "
                    "and the import order decides which names exist at load time."
                ),
                recommendation=(
                    "Move the code both sides need into a module that imports neither, "
                    f"and have {first.from_path} import that instead of "
                    f"{first.to_path}."
                ),
                evidence_quote=first.statement or None,
            )
        )
    return tuple(findings)


def build_crew(
    repository_tools: RepositoryTools,
    llm: GatewayLLM,
    facts: ArchitectureFacts,
    calls: list[str],
) -> Any:
    """One agent, one task: judge the facts, then report what matters."""
    from crewai import Agent, Crew, Process, Task

    prompt = load_prompt("architecture")
    reviewer = Agent(
        role=prompt["role"],
        goal=prompt["goal"],
        backstory=prompt["backstory"],
        tools=read_only_tools(repository_tools, calls),
        llm=crew_llm(llm),
        allow_delegation=False,
        max_iter=MAX_ITERATIONS,
        verbose=False,
    )

    def reviewed_before_answering(output):
        """Three checks, in the order that makes the feedback useful.

        A cycle is a fact, so it can be reported from the facts alone - but it also
        *must* be reported, because a crew that silently drops the one thing the
        graph proved has failed at its easiest job. "Too large" and "wrong layer"
        are judgements, so they need code behind them. And an answer that is not the
        contract's JSON costs a retry rather than a whole stage, which is what turned
        the js_app cycle into an unparseable reply in the first full run.
        """
        raw = getattr(output, "raw", output) or ""
        try:
            report = parse_report(str(raw))
        except InvalidOutput:
            return False, (
                "That is not the JSON object from the contract. Reply with "
                '{"findings": [...]} and nothing else - no prose, no Markdown list.'
            )
        if report.findings and "retrieve_context" not in calls:
            return False, (
                "You have not read any code. Call retrieve_context with a question "
                "about the modules you are about to cite (for example the largest "
                "module or one end of a cycle), read what it returns, and only then "
                "answer."
            )
        return True, output

    review = Task(
        description=fill(prompt["review"], facts=facts.render()),
        expected_output=prompt["json_contract"],
        agent=reviewer,
        guardrail=reviewed_before_answering,
        guardrail_max_retries=2,
    )
    return Crew(agents=[reviewer], tasks=[review], process=Process.sequential, verbose=False)


@dataclass(frozen=True)
class ArchitectureOutcome(AgentOutcome):
    """The baseline's outcome plus the shape the crew was shown."""

    modules: int = 0
    imports: int = 0
    cycles: int = 0
    entry_points: tuple[str, ...] = field(default_factory=tuple)
    large_modules: tuple[str, ...] = field(default_factory=tuple)
    layer_edges: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        """JSON-friendly form for ``eval_runs.metrics``."""
        return {
            **super().as_dict(),
            "modules": self.modules,
            "imports": self.imports,
            "cycles": self.cycles,
            "entry_points": list(self.entry_points),
            "large_modules": list(self.large_modules),
            "layer_edges": self.layer_edges,
            "notes": list(self.notes),
        }


def run_architecture_agent(
    session: Session,
    audit_id: uuid.UUID,
    *,
    client: LLMClient | None = None,
    tools: RepositoryTools | None = None,
    crew_factory: Any = None,
) -> ArchitectureOutcome:
    """Run the architecture crew over an indexed audit."""
    repository_tools = tools or RepositoryTools(session, audit_id)
    if not ensure_index(session, audit_id, repository_tools):
        return ArchitectureOutcome(errors=("no readable snapshot to index",))

    facts = read_facts(session, audit_id, repository_tools)
    model_client = client or LLMClient(session, audit_id)
    llm = GatewayLLM(model_client, agent=AGENT_ROLE)
    calls: list[str] = []
    started = time.monotonic()

    proved = cycle_findings(facts)

    try:
        crew = (crew_factory or build_crew)(repository_tools, llm, facts, calls)
        result = crew.kickoff()
    except LLMError as exc:
        logger.warning("architecture crew could not call the model for %s: %s", audit_id, exc)
        stored = _store_proved(session, audit_id, proved, repository_tools)
        return ArchitectureOutcome(
            calls=llm.calls,
            accepted=stored,
            cycles=len(facts.cycles),
            errors=(str(exc),),
            notes=("crew aborted", _proved_note(stored)),
        )
    except Exception as exc:  # noqa: BLE001 - a crew failure must not fail the audit
        logger.warning("architecture crew failed for %s: %s", audit_id, exc)
        stored = _store_proved(session, audit_id, proved, repository_tools)
        return ArchitectureOutcome(
            calls=llm.calls,
            accepted=stored,
            cycles=len(facts.cycles),
            errors=(f"{type(exc).__name__}: {exc}"[:300],),
            notes=("crew aborted", _proved_note(stored)),
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    answer = _task_output(result, 0)
    logger.debug("architecture answer: %s", answer[:4_000])

    try:
        report = parse_report(answer)
    except InvalidOutput as exc:
        logger.info("architecture answer unparseable for %s: %s", audit_id, exc)
        stored = _store_proved(session, audit_id, proved, repository_tools)
        return ArchitectureOutcome(
            calls=llm.calls,
            accepted=stored,
            latency_ms=latency_ms,
            cycles=len(facts.cycles),
            rejected=1,
            dropped=1,
            notes=(f"answer unparseable: {exc}"[:200], _proved_note(stored)),
        )

    validation = validate_architecture_report(report, repository_tools)
    named = {finding.file for finding in validation.accepted}
    unstated = tuple(
        finding
        for finding, cycle in zip(proved, facts.cycles)
        if not named & set(cycle.paths)
    )
    accepted: tuple[AgentFinding, ...] = validation.accepted + (
        validate_architecture_report(AgentReport(findings=list(unstated)), repository_tools).accepted
        if unstated
        else ()
    )
    missing_files = sum(
        1 for item in validation.rejected if "is not in this repository" in item.reason
    )
    stored = store_findings(
        session, audit_id, accepted, repository_tools, source_tool=SOURCE_TOOL
    )

    notes = []
    if validation.rejected:
        notes.append(f"{len(validation.rejected)} finding(s) rejected by the validator")
    if missing_files:
        notes.append(f"{missing_files} cited a file this repository does not have")
    if unstated:
        notes.append(f"{len(unstated)} cycle(s) reported from the graph, unmatched by the model")
    if stored:
        notes.append(f"added {stored} new finding(s)")
    logger.info(
        "architecture crew for audit %s: %d cycles, %d stored, %d rejected, %d calls",
        audit_id,
        len(facts.cycles),
        stored,
        len(validation.rejected),
        llm.calls,
    )
    return ArchitectureOutcome(
        model=llm.model,
        calls=llm.calls,
        accepted=stored,
        rejected=len(validation.rejected),
        latency_ms=latency_ms,
        modules=facts.modules,
        imports=facts.imports,
        cycles=len(facts.cycles),
        entry_points=facts.entry_points,
        large_modules=tuple(module.path for module in facts.large_modules),
        layer_edges=len(facts.layer_edges),
        notes=tuple(notes),
    )


def _store_proved(
    session: Session, audit_id: uuid.UUID, proved: tuple[AgentFinding, ...], tools: RepositoryTools
) -> int:
    """Store the cycles the graph proved, when the crew itself produced nothing."""
    if not proved:
        return 0
    return store_findings(session, audit_id, proved, tools, source_tool=SOURCE_TOOL)


def _proved_note(stored: int) -> str:
    return f"{stored} cycle(s) reported from the graph" if stored else "no cycle to report"


def _task_output(crew_result: Any, index: int) -> str:
    """The raw text of one task's output, whichever shape CrewAI returned."""
    outputs = getattr(crew_result, "tasks_output", None) or []
    if index < len(outputs):
        return getattr(outputs[index], "raw", str(outputs[index])) or ""
    return ""
