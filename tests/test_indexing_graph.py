"""Step 2.2: call edges, module edges and circular dependency detection."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.db.enums import AuditStatus, SourceType
from app.db.models import Audit, CallEdge, ModuleEdge, Repository, Symbol
from app.db.session import get_session_factory
from app.indexing.graph import (
    SymbolIndex,
    detect_module_cycles,
    find_cycles,
    module_edges,
    resolve_calls,
    strongly_connected_components,
)
from app.indexing.parser import ParsedFile, parse_file
from app.indexing.service import index_audit, index_graph
from app.intake.filtering import index_snapshot

FIXTURES = Path(__file__).resolve().parents[1] / "benchmark" / "repos"


def load_repo(root: Path) -> tuple[list[ParsedFile], dict[str, str]]:
    parsed: list[ParsedFile] = []
    sources: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8", errors="replace")
        sources[relative] = source
        parsed.append(parse_file(path, source=source, display_path=relative))
    return parsed, sources


# --- the planted cycles ----------------------------------------------------


def test_planted_python_cycle_is_reported():
    parsed, _ = load_repo(FIXTURES / "python_app")
    assert detect_module_cycles(parsed) == [["app/billing.py", "app/orders.py"]]


def test_planted_javascript_cycle_is_reported():
    parsed, _ = load_repo(FIXTURES / "js_app")
    assert detect_module_cycles(parsed) == [["src/lib/queue.js", "src/lib/worker.js"]]


def test_a_repository_without_a_cycle_returns_none(tmp_path):
    root = tmp_path / "acyclic"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "a.py").write_text("from pkg.b import run\n\ndef call():\n    return run()\n", encoding="utf-8")
    (root / "pkg" / "b.py").write_text("from pkg.c import helper\n\ndef run():\n    return helper()\n", encoding="utf-8")
    (root / "pkg" / "c.py").write_text("def helper():\n    return 1\n", encoding="utf-8")

    parsed, _ = load_repo(root)

    assert detect_module_cycles(parsed) == []
    assert len(module_edges(parsed)) == 2


# --- Tarjan / cycle shape --------------------------------------------------


def test_self_import_is_a_one_file_cycle():
    assert find_cycles(["a.py", "b.py"], [("a.py", "a.py"), ("b.py", "a.py")]) == [["a.py"]]


def test_two_independent_cycles_are_both_reported():
    edges = [
        ("a", "b"),
        ("b", "a"),
        ("c", "d"),
        ("d", "e"),
        ("e", "c"),
        ("e", "a"),  # a tail into the first cycle is not itself a cycle
    ]
    cycles = find_cycles(["a", "b", "c", "d", "e"], edges)

    assert cycles == [["a", "b"], ["c", "d", "e"]]


def test_a_component_is_a_cycle_only_when_it_closes():
    assert find_cycles(["a", "b", "c"], [("a", "b"), ("b", "c")]) == []
    assert strongly_connected_components(["a", "b", "c"], [("a", "b"), ("b", "c")]) == [
        ["a"],
        ["b"],
        ["c"],
    ]


def test_a_cycle_chain_of_many_files_is_ordered():
    names = [f"m{index}.py" for index in range(6)]
    edges = [(names[index], names[(index + 1) % len(names)]) for index in range(len(names))]

    assert find_cycles(names, edges) == [names]


def test_deep_chain_does_not_recurse_to_death():
    names = [f"n{index}.py" for index in range(2_000)]
    edges = [(names[index], names[index + 1]) for index in range(len(names) - 1)]

    assert find_cycles(names, edges) == []


# --- module edges ----------------------------------------------------------


def test_python_imports_resolve_to_files_and_external_imports_are_skipped():
    parsed, _ = load_repo(FIXTURES / "python_app")
    edges = {(edge.from_path, edge.to_path) for edge in module_edges(parsed)}

    assert ("app/orders.py", "app/billing.py") in edges
    assert ("app/billing.py", "app/orders.py") in edges
    assert ("app/routes/search.py", "app/auth.py") in edges
    assert ("app/routes/search.py", "app/database.py") in edges
    # fastapi, os, MySQLdb and dataclasses are not repository files.
    assert all("fastapi" not in target for _, target in edges)


def test_relative_javascript_require_resolves_through_index_files():
    parsed, _ = load_repo(FIXTURES / "js_app")
    edges = {(edge.from_path, edge.to_path) for edge in module_edges(parsed)}

    assert ("src/routes/admin.js", "src/db/index.js") in edges
    assert ("src/lib/queue.js", "src/lib/worker.js") in edges
    assert ("src/lib/worker.js", "src/lib/queue.js") in edges
    assert all("express" not in target for _, target in edges)


# --- call resolution -------------------------------------------------------


def test_calls_resolve_across_modules_through_imports():
    parsed, sources = load_repo(FIXTURES / "python_app")
    calls = {
        (call.caller_path, call.caller_name, call.callee_name): call
        for call in resolve_calls(parsed, sources)
    }

    # app/orders.py imports calculate_total from app/billing
    cross_module = calls[("app/orders.py", "summarize", "calculate_total")]
    assert cross_module.callee_key == ("app/billing.py", "calculate_total")

    # app/routes/search.py imports search_orders from app/database
    route_call = calls[("app/routes/search.py", "search", "search_orders")]
    assert route_call.callee_key == ("app/database.py", "search_orders")

    # same-file call
    same_file = calls[("app/database.py", "get_order", "get_connection")]
    assert same_file.callee_key == ("app/database.py", "get_connection")


def test_unresolved_calls_keep_their_name():
    parsed, sources = load_repo(FIXTURES / "python_app")
    calls = resolve_calls(parsed, sources)

    library_call = next(call for call in calls if call.callee_name == "MySQLdb.connect")
    assert library_call.callee_key is None
    assert library_call.resolved is False
    assert library_call.callee_name == "MySQLdb.connect"


def test_builtins_and_self_calls_are_not_edges():
    source = '''
class Repo:
    def load(self, key):
        return self.fetch(key)

    def fetch(self, key):
        return len(key)
'''
    parsed = parse_file("app/repo.py", source=source, language="python")
    calls = resolve_calls([parsed], {"app/repo.py": source})

    names = [call.callee_name for call in calls]
    assert "len" not in names
    assert "self.fetch" in names
    fetch = next(call for call in calls if call.callee_name == "self.fetch")
    assert fetch.callee_key == ("app/repo.py", "fetch")


def test_javascript_calls_resolve_inside_the_repository():
    parsed, sources = load_repo(FIXTURES / "js_app")
    calls = resolve_calls(parsed, sources)

    enqueue_to_worker = next(
        call
        for call in calls
        if call.caller_path == "src/lib/queue.js" and call.callee_name == "processJob"
    )
    assert enqueue_to_worker.callee_key == ("src/lib/worker.js", "processJob")


def test_symbol_index_prefers_the_imported_module_over_a_same_named_symbol():
    parsed, _ = load_repo(FIXTURES / "python_app")
    index = SymbolIndex(parsed)

    assert index.resolve("app/routes/search.py", "search_orders") == (
        "app/database.py",
        "search_orders",
    )
    # An unknown name is not invented.
    assert index.resolve("app/routes/search.py", "not_a_symbol") is None


# --- persistence -----------------------------------------------------------


def _seed(tmp_path, repo_name: str) -> tuple[Repository, Audit, Path]:
    source_root = FIXTURES / repo_name
    target = tmp_path / f"snapshot-{uuid.uuid4().hex}"
    for path in source_root.rglob("*"):
        if path.is_file():
            destination = target / path.relative_to(source_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    session = get_session_factory()()
    repository = Repository(
        name=f"pytest-graph-{uuid.uuid4().hex[:8]}",
        source_type=SourceType.ZIP,
        storage_path=str(target),
    )
    session.add(repository)
    session.commit()
    session.refresh(repository)
    audit = Audit(repository_id=repository.id, status=AuditStatus.QUEUED)
    session.add(audit)
    session.commit()
    session.refresh(audit)
    session.close()
    return repository, audit, target


@pytest.mark.parametrize(
    "repo_name,expected_cycle",
    [
        ("python_app", ["app/billing.py", "app/orders.py"]),
        ("js_app", ["src/lib/queue.js", "src/lib/worker.js"]),
    ],
)
def test_graph_rows_are_stored_and_cycles_are_reported(tmp_path, repo_name, expected_cycle):
    repository, audit, root = _seed(tmp_path, repo_name)

    with get_session_factory()() as session:
        try:
            index_snapshot(session, audit.id, root)
            index_audit(session, audit.id, snapshot_root=root)
            summary = index_graph(session, audit.id, snapshot_root=root)

            assert [list(cycle) for cycle in summary.cycles] == [expected_cycle]
            assert summary.module_edges > 0
            assert summary.call_edges > 0
            assert summary.resolved_calls > 0

            stored_module_edges = session.execute(
                select(func.count()).select_from(ModuleEdge).where(ModuleEdge.audit_id == audit.id)
            ).scalar_one()
            stored_call_edges = session.execute(
                select(func.count()).select_from(CallEdge).where(CallEdge.audit_id == audit.id)
            ).scalar_one()
            assert stored_module_edges == summary.module_edges
            assert stored_call_edges == summary.call_edges

            unresolved = session.execute(
                select(func.count())
                .select_from(CallEdge)
                .where(CallEdge.audit_id == audit.id, CallEdge.callee_symbol_id.is_(None))
            ).scalar_one()
            assert unresolved == summary.unresolved_calls
            assert unresolved > 0, "library calls should stay unresolved rather than be guessed"

            # Every caller points at a symbol in the same audit.
            callers = session.execute(
                select(CallEdge.caller_symbol_id).where(CallEdge.audit_id == audit.id)
            ).scalars().all()
            symbol_ids = set(
                session.execute(
                    select(Symbol.id).where(Symbol.audit_id == audit.id)
                ).scalars()
            )
            assert set(callers) <= symbol_ids

            # Re-running replaces instead of appending.
            again = index_graph(session, audit.id, snapshot_root=root)
            assert again.call_edges == summary.call_edges
            assert (
                session.execute(
                    select(func.count())
                    .select_from(CallEdge)
                    .where(CallEdge.audit_id == audit.id)
                ).scalar_one()
                == summary.call_edges
            )
        finally:
            session.delete(session.get(Audit, audit.id))
            session.delete(session.get(Repository, repository.id))
            session.commit()
