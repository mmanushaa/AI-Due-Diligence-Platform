"""Step 2.1: syntax-aware parsing, symbol extraction and chunking."""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.db.enums import AuditStatus, SourceType, SymbolKind
from app.db.models import Audit, Chunk, File as FileModel, Repository, Symbol
from app.db.session import get_session_factory
from app.indexing.parser import (
    DEFAULT_MAX_CHUNK_TOKENS,
    ParsedSymbol,
    chunk_symbol,
    estimate_tokens,
    parse_file,
    parse_python,
    parse_snapshot,
)
from app.indexing.service import index_audit

FIXTURES = Path(__file__).resolve().parents[1] / "benchmark" / "repos"
PYTHON_APP = FIXTURES / "python_app"
JS_APP = FIXTURES / "js_app"


def kinds(parsed) -> dict[str, int]:
    counts: dict[str, int] = {}
    for symbol in parsed.symbols:
        counts[symbol.kind.value] = counts.get(symbol.kind.value, 0) + 1
    return counts


def by_name(parsed) -> dict[str, ParsedSymbol]:
    return {symbol.name: symbol for symbol in parsed.symbols}


# --- Python: exact line ranges ---------------------------------------------


def _ast_definitions(source: str) -> list[tuple[str, str, int, int]]:
    """Independent ground truth: every def/class with its ast line range."""
    tree = ast.parse(source)
    found: list[tuple[str, str, int, int]] = []

    def walk(node: ast.AST, *, in_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                found.append(("class", child.name, child.lineno, child.end_lineno))
                walk(child, in_class=True)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if in_class else "function"
                found.append((kind, child.name, child.lineno, child.end_lineno))
                walk(child, in_class=in_class)
            else:
                walk(child, in_class=in_class)

    walk(tree, in_class=False)
    return found


@pytest.mark.parametrize(
    "relative_path",
    [
        "app/auth.py",
        "app/billing.py",
        "app/config.py",
        "app/database.py",
        "app/main.py",
        "app/orders.py",
        "app/routes/orders.py",
        "app/routes/search.py",
    ],
)
def test_every_python_definition_has_the_ast_line_range(relative_path):
    path = PYTHON_APP / relative_path
    source = path.read_text(encoding="utf-8")
    parsed = parse_file(path)

    assert parsed.error is None
    definition_symbols = [
        symbol
        for symbol in parsed.symbols
        if symbol.kind in {SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS, SymbolKind.ROUTE}
    ]
    found = {
        (symbol.kind.value if symbol.kind is not SymbolKind.ROUTE else "function", symbol.name): (
            symbol.line_start,
            symbol.line_end,
        )
        for symbol in definition_symbols
    }

    expected = {
        (kind, name): (start, end) for kind, name, start, end in _ast_definitions(source)
    }

    assert found == expected
    assert len(definition_symbols) == len(expected)


def test_every_python_import_is_found_with_its_range():
    path = PYTHON_APP / "app/database.py"
    parsed = parse_file(path)
    imports = [symbol for symbol in parsed.symbols if symbol.kind is SymbolKind.IMPORT]

    assert imports, "expected import symbols"
    for symbol in imports:
        line = path.read_text(encoding="utf-8").splitlines()[symbol.line_start - 1]
        assert line.lstrip().startswith(("import ", "from ")), line
        assert symbol.line_start <= symbol.line_end


def test_python_symbols_are_not_duplicated():
    parsed = parse_file(PYTHON_APP / "app/orders.py")
    keys = [(symbol.name, symbol.kind, symbol.line_start) for symbol in parsed.symbols]
    assert len(keys) == len(set(keys))


# --- Python: routes, decorators, auth markers ------------------------------


def test_fastapi_route_is_detected_with_path_and_decorator():
    parsed = parse_file(PYTHON_APP / "app/routes/orders.py")
    route = by_name(parsed)["read_order"]

    assert route.kind is SymbolKind.ROUTE
    assert (route.line_start, route.line_end) == (11, 16)
    assert route.metadata["framework"] == "fastapi"
    assert route.metadata["route"]["path"] == "/{order_id}"
    assert route.metadata["route"]["methods"] == ["GET"]
    assert route.metadata["decorators"] == ["router.get"]


def test_route_signature_excludes_the_body():
    parsed = parse_file(PYTHON_APP / "app/routes/orders.py")
    route = by_name(parsed)["read_order"]
    assert route.signature == "def read_order(order_id: str) -> dict"


def test_flask_routes_and_auth_dependencies_are_recorded():
    source = '''
from flask import Blueprint
from fastapi import Depends

bp = Blueprint("admin", __name__)


@bp.route("/admin/users", methods=["GET", "POST"])
def list_users():
    return []


@bp.post("/admin/users/<int:user_id>")
def update_user(user_id, current_user=Depends(require_admin)):
    return {}
'''
    parsed = parse_python(source, "app/admin.py")
    symbols = by_name(parsed)

    listed = symbols["list_users"]
    assert listed.kind is SymbolKind.ROUTE
    assert listed.metadata["framework"] == "flask"
    assert listed.metadata["route"]["methods"] == ["GET", "POST"]
    assert listed.metadata["route"]["path"] == "/admin/users"

    updated = symbols["update_user"]
    assert updated.metadata["route"]["methods"] == ["POST"]
    assert "current_user" in updated.metadata["auth_markers"]


def test_fastapi_dependencies_kwarg_is_an_auth_marker():
    source = '''
from fastapi import APIRouter, Depends

router = APIRouter()


@router.get("/reports", dependencies=[Depends(require_admin)])
def list_reports():
    return []
'''
    parsed = parse_python(source, "app/reports.py")
    route = by_name(parsed)["list_reports"]
    assert route.metadata["auth_markers"] == ["require_admin"]


def test_a_plain_function_call_named_like_a_verb_is_not_a_route():
    source = '''
def run(client, key):
    return client.get(key)
'''
    parsed = parse_python(source, "app/plain.py")
    assert [symbol.kind for symbol in parsed.symbols] == [SymbolKind.FUNCTION]


# --- configuration files ---------------------------------------------------


def test_requirements_txt_is_one_config_symbol():
    parsed = parse_file(PYTHON_APP / "requirements.txt")
    assert len(parsed.symbols) == 1
    symbol = parsed.symbols[0]
    assert symbol.kind is SymbolKind.CONFIG
    assert symbol.name == "requirements.txt"
    assert symbol.line_start == 1
    assert symbol.line_end == len((PYTHON_APP / "requirements.txt").read_text().splitlines())


def test_package_json_config_symbol_lists_dependencies():
    parsed = parse_file(JS_APP / "package.json")
    config = parsed.symbols[0]
    assert config.kind is SymbolKind.CONFIG
    assert "lodash" in config.metadata["dependencies"]


# --- JavaScript / TypeScript ----------------------------------------------


def test_express_route_is_detected_with_path_and_line_range():
    parsed = parse_file(JS_APP / "src/routes/admin.js")
    routes = [symbol for symbol in parsed.symbols if symbol.kind is SymbolKind.ROUTE]

    assert len(routes) == 1
    route = routes[0]
    assert (route.line_start, route.line_end) == (9, 15)
    assert route.metadata["route"]["framework"] == "express"
    assert route.metadata["route"]["path"] == "/users/:id"
    assert route.metadata["route"]["methods"] == ["GET"]


def test_express_middleware_names_are_kept_and_auth_is_flagged():
    source = """
const router = require("express").Router();

router.get("/admin/x", requireAuth, isAdmin, (req, res) => {
  res.json({ ok: true });
});
"""
    parsed = parse_file("app/admin.js", source=source, language="javascript")
    route = parsed.symbols[0]

    assert route.kind is SymbolKind.ROUTE
    assert route.metadata["route"]["middleware"] == ["requireAuth", "isAdmin"]
    # Both middleware names look like authorization guards.
    assert route.metadata["route"]["auth_markers"] == ["isAdmin", "requireAuth"]


def test_named_js_function_and_imports_have_correct_ranges():
    path = JS_APP / "src/lib/queue.js"
    parsed = parse_file(path)
    lines = path.read_text(encoding="utf-8").splitlines()

    enqueue = by_name(parsed)["enqueue"]
    assert enqueue.kind is SymbolKind.FUNCTION
    assert (enqueue.line_start, enqueue.line_end) == (7, 10)
    assert lines[enqueue.line_start - 1].startswith("function enqueue")
    assert enqueue.metadata["parameters"] == ["job"]

    imports = [symbol for symbol in parsed.symbols if symbol.kind is SymbolKind.IMPORT]
    assert len(imports) == 1
    assert imports[0].name == "./worker"
    assert imports[0].metadata["imported_names"] == ["processJob"]
    assert imports[0].line_start == 3


def test_commonjs_imports_are_found_in_server_entrypoint():
    parsed = parse_file(JS_APP / "server.js")
    imports = [symbol for symbol in parsed.symbols if symbol.kind is SymbolKind.IMPORT]

    assert [symbol.name for symbol in imports] == [
        "express",
        "./src/auth/requireAuth",
        "./src/routes/admin",
        "./src/routes/reports",
        "./src/routes/users",
    ]
    assert [symbol.line_start for symbol in imports] == [3, 4, 5, 6, 7]
    # `app.listen(port, () => ...)` is an inline callback, not a symbol.
    assert [symbol.kind for symbol in parsed.symbols if symbol.kind is SymbolKind.FUNCTION] == []


def test_class_and_methods_are_extracted_from_javascript():
    source = """
export class Ledger {
  constructor(client) {
    this.client = client;
  }

  async record(entry) {
    return this.client.insert(entry);
  }
}
"""
    parsed = parse_file("src/ledger.js", source=source, language="javascript")
    symbols = by_name(parsed)

    assert symbols["Ledger"].kind is SymbolKind.CLASS
    assert (symbols["Ledger"].line_start, symbols["Ledger"].line_end) == (2, 10)
    assert symbols["record"].kind is SymbolKind.METHOD
    assert (symbols["record"].line_start, symbols["record"].line_end) == (7, 9)


def test_typescript_arrows_classes_and_routes_are_parsed():
    source = """
import express, { Request, Response } from "express";
import { verifyToken } from "./auth";

const router = express.Router();

export const health = (): string => "ok";

router.get("/private", verifyToken, async (req: Request, res: Response) => {
  res.json({ user: req.user });
});

export class Widget {
  render(width: number): string {
    return "w".repeat(width);
  }
}
"""
    parsed = parse_file("src/api.ts", source=source, language="typescript")
    symbols = by_name(parsed)

    assert parsed.error is None
    assert symbols["health"].kind is SymbolKind.FUNCTION
    assert (symbols["health"].line_start, symbols["health"].line_end) == (7, 7)

    route = symbols["GET /private"]
    assert route.kind is SymbolKind.ROUTE
    assert route.metadata["route"]["middleware"] == ["verifyToken"]
    assert route.metadata["route"]["auth_markers"] == ["verifyToken"]
    assert (route.line_start, route.line_end) == (9, 11)

    assert symbols["Widget"].kind is SymbolKind.CLASS
    assert symbols["render"].kind is SymbolKind.METHOD

    import_names = [symbol.name for symbol in parsed.symbols if symbol.kind is SymbolKind.IMPORT]
    assert import_names == ["express", "./auth"]


def test_tsx_uses_the_tsx_grammar():
    source = """
type Props = { label: string };

export function Badge({ label }: Props) {
  return <span>{label}</span>;
}
"""
    parsed = parse_file("src/Badge.tsx", source=source, language="typescript")
    assert parsed.error is None
    assert by_name(parsed)["Badge"].kind is SymbolKind.FUNCTION


# --- chunks ----------------------------------------------------------------


def test_every_symbol_gets_exactly_one_chunk_when_it_fits():
    parsed = parse_file(PYTHON_APP / "app/routes/orders.py")

    assert len(parsed.chunks) == len(parsed.symbols)
    for symbol, chunk in zip(parsed.symbols, parsed.chunks):
        assert (chunk.line_start, chunk.line_end) == (symbol.line_start, symbol.line_end)
        assert chunk.token_count == estimate_tokens(chunk.content)
        assert chunk.content.strip()


def test_chunk_content_matches_the_source_lines():
    path = PYTHON_APP / "app/database.py"
    lines = path.read_text(encoding="utf-8").splitlines()
    parsed = parse_file(path)

    for chunk in parsed.chunks:
        expected = "\n".join(lines[chunk.line_start - 1 : chunk.line_end])
        assert chunk.content == expected


def test_a_large_function_is_split_at_statement_boundaries():
    body = "\n".join(
        f"    total += values[{index}]  # padding comment to grow the statement" for index in range(60)
    )
    source = f'''
def accumulate(values):
    total = 0
{body}
    return total
'''
    parsed = parse_file("app/big.py", source=source, language="python", max_chunk_tokens=400)
    symbol = parsed.symbols[0]

    assert len(parsed.chunks) > 1
    assert all(chunk.token_count <= 400 for chunk in parsed.chunks)

    lines = source.splitlines()
    statement_starts = {
        number for number, line in enumerate(lines, start=1) if line.strip().startswith("total +=")
    }
    # Every chunk after the first starts on a statement, never inside one.
    for chunk in parsed.chunks[1:]:
        assert chunk.line_start in statement_starts
    # Chunks are contiguous and cover the function once.
    assert parsed.chunks[0].line_start == symbol.line_start
    assert parsed.chunks[-1].line_end == symbol.line_end
    for previous, current in zip(parsed.chunks, parsed.chunks[1:]):
        assert current.line_start == previous.line_end + 1


def test_a_small_function_is_never_split():
    source = '''
def small(a):
    return a + 1
'''
    parsed = parse_python(source, "app/small.py")
    function = next(symbol for symbol in parsed.symbols if symbol.name == "small")
    chunks = chunk_symbol(function, source.splitlines(), max_chunk_tokens=10_000)
    assert len(chunks) == 1
    assert (chunks[0].line_start, chunks[0].line_end) == (function.line_start, function.line_end)


def test_unparsable_code_keeps_a_file_chunk_and_reports_the_error():
    parsed = parse_python("def broken(:\n    pass\n", "app/broken.py")
    assert parsed.error is not None and "SyntaxError" in parsed.error
    assert len(parsed.chunks) == 1
    assert parsed.chunks[0].line_start == 1


# --- snapshots -------------------------------------------------------------


def test_snapshot_parsing_covers_both_fixtures():
    python_files = list(parse_snapshot(PYTHON_APP))
    js_files = list(parse_snapshot(JS_APP))

    assert len(python_files) == 11
    assert len(js_files) == 10
    assert all(parsed.error is None for parsed in python_files + js_files)
    assert sum(len(parsed.chunks) for parsed in python_files) > 0
    assert sum(len(parsed.chunks) for parsed in js_files) > 0


def test_python_app_symbol_census():
    """The fixture's expected shape, kept explicit so a regression is visible."""
    census: dict[str, dict[str, int]] = {}
    for parsed in parse_snapshot(PYTHON_APP):
        if parsed.symbols:
            census[parsed.path] = kinds(parsed)

    assert census["app/auth.py"] == {"import": 1, "function": 1}
    assert census["app/billing.py"] == {"import": 1, "function": 2}
    assert census["app/config.py"] == {"import": 1, "class": 1}
    assert census["app/database.py"] == {"import": 2, "function": 3}
    assert census["app/main.py"] == {"import": 2}
    assert census["app/orders.py"] == {"import": 2, "class": 1, "function": 2}
    assert census["app/routes/orders.py"] == {"import": 2, "route": 1}
    assert census["app/routes/search.py"] == {"import": 3, "route": 1}
    assert census["requirements.txt"] == {"config": 1}


# --- persistence -----------------------------------------------------------


def _create_repository_and_audit(session, storage_path: str) -> tuple[Repository, Audit]:
    repository = Repository(
        name=f"pytest-index-{uuid.uuid4().hex[:8]}",
        source_type=SourceType.ZIP,
        storage_path=storage_path,
    )
    session.add(repository)
    session.commit()
    session.refresh(repository)

    audit = Audit(repository_id=repository.id, status=AuditStatus.QUEUED)
    session.add(audit)
    session.commit()
    session.refresh(audit)
    return repository, audit


def test_symbols_and_chunks_are_stored_and_reruns_do_not_duplicate(tmp_path):
    from app.intake.filtering import index_snapshot

    root = tmp_path / f"snapshot-{uuid.uuid4().hex}"
    for relative in ("app/config.py", "app/routes/orders.py", "requirements.txt"):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((PYTHON_APP / relative).read_text(encoding="utf-8"), encoding="utf-8")

    with get_session_factory()() as session:
        repository, audit = _create_repository_and_audit(session, str(root))
        try:
            index_snapshot(session, audit.id, root)
            first = index_audit(session, audit.id, snapshot_root=root)
            second = index_audit(session, audit.id, snapshot_root=root)

            assert first.symbols == second.symbols
            assert first.chunks == second.chunks
            assert first.errors == ()

            stored_symbols = session.execute(
                select(func.count()).select_from(Symbol).where(Symbol.audit_id == audit.id)
            ).scalar_one()
            stored_chunks = session.execute(
                select(func.count()).select_from(Chunk).where(Chunk.audit_id == audit.id)
            ).scalar_one()
            assert stored_symbols == first.symbols
            assert stored_chunks == first.chunks

            route = session.execute(
                select(Symbol).where(
                    Symbol.audit_id == audit.id, Symbol.kind == SymbolKind.ROUTE
                )
            ).scalar_one()
            assert route.name == "read_order"
            assert route.symbol_metadata["route"]["path"] == "/{order_id}"

            # Every symbol-backed chunk points at its symbol and has a tsvector.
            chunks = session.execute(
                select(Chunk).where(Chunk.audit_id == audit.id, Chunk.symbol_id.is_not(None))
            ).scalars().all()
            assert chunks
            assert all(chunk.content_tsv is not None for chunk in chunks)
            assert all(chunk.token_count > 0 for chunk in chunks)

            excluded_file = session.execute(
                select(FileModel).where(FileModel.audit_id == audit.id).limit(1)
            ).scalar_one()
            assert excluded_file.is_excluded is False
        finally:
            session.delete(session.get(Audit, audit.id))
            session.delete(session.get(Repository, repository.id))
            session.commit()
