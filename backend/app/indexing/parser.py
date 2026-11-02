"""Syntax-aware parsing and chunking (step 2.1).

Three extractors share one output shape:

* Python is parsed with the standard library's ``ast`` module, which gives exact
  line numbers, decorators and the argument list without any grammar build.
* JavaScript and TypeScript are parsed with Tree-sitter grammars, which handle
  the syntax Python's ``ast`` cannot describe (arrow functions, class fields,
  ``export`` wrappers, Express route calls).
* Configuration manifests (``requirements.txt``, ``package.json``, ``.toml``,
  ``.yaml``, ``Dockerfile`` ...) carry no functions or classes, so each is
  recorded as a single ``config`` symbol covering the file.

Nothing here touches the database: :func:`parse_file` and :func:`parse_snapshot`
return plain dataclasses, and ``app.indexing.service`` stores them.

Chunking rule: one chunk per symbol. A symbol is only split when its content
exceeds ``max_chunk_tokens``, and then the split happens on statement
boundaries — never in the middle of a function body unless that single
statement is itself over the limit.

Token counts are an estimate, not a model tokenizer: ``ceil(characters / 4)``.
The value is used to decide when to split and to budget retrieval context, so a
stable, dependency-free approximation is preferable to a tokenizer tied to one
provider.
"""

from __future__ import annotations

import ast
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from tree_sitter import Language, Node, Parser

import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript

from app.db.enums import SymbolKind

DEFAULT_MAX_CHUNK_TOKENS = 1_200

# --- languages -------------------------------------------------------------

LANGUAGE_PYTHON = "python"
LANGUAGE_JAVASCRIPT = "javascript"
LANGUAGE_TYPESCRIPT = "typescript"

PYTHON_SUFFIXES = frozenset({".py", ".pyi"})
JAVASCRIPT_SUFFIXES = frozenset({".js", ".jsx", ".mjs", ".cjs"})
TYPESCRIPT_SUFFIXES = frozenset({".ts", ".tsx"})

# Files that are configuration rather than code. Matched on the lowercased name.
CONFIG_FILENAMES = frozenset(
    {
        "requirements.txt",
        "constraints.txt",
        "package.json",
        "package-lock.json",
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "tox.ini",
        "alembic.ini",
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
        ".env",
        ".env.example",
        ".env.sample",
        "makefile",
        "nginx.conf",
    }
)
CONFIG_SUFFIXES = frozenset({".ini", ".cfg", ".toml", ".yaml", ".yml", ".env", ".conf"})

HTTP_VERBS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "route"})

# Names that look like an authorization or authentication dependency when they
# appear in a decorator's kwargs or in a handler's parameter list.
AUTH_MARKER_PATTERN = re.compile(
    r"(auth|token|jwt|login|session|permission|role|admin|scope|guard|verify|current_user|principal)",
    re.IGNORECASE,
)

# Router-like receivers for FastAPI/Flask route decorators.
ROUTER_OBJECTS = frozenset(
    {"app", "router", "api", "bp", "blueprint", "application", "server", "routes"}
)

# Middleware/parameter names that suggest a route is protected.
EXPRESS_MIDDLEWARE_HINT = re.compile(r"(auth|guard|admin|protect|permission|role|verify)", re.IGNORECASE)


# --- shared dataclasses ----------------------------------------------------


@dataclass(frozen=True)
class ParsedSymbol:
    """One row destined for the ``symbols`` table."""

    name: str
    kind: SymbolKind
    line_start: int
    line_end: int
    signature: str | None = None
    metadata: dict = field(default_factory=dict)
    # Line spans of the statements inside the symbol, used to split a symbol
    # that is too large for one chunk. Empty for symbols that cannot be split
    # (imports, config files).
    statement_spans: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class ParsedChunk:
    """One row destined for the ``chunks`` table."""

    line_start: int
    line_end: int
    content: str
    token_count: int
    # Index into ParsedFile.symbols, or None for a chunk that is not tied to a
    # single symbol.
    symbol_index: int | None = None


@dataclass(frozen=True)
class ParsedFile:
    path: str
    language: str | None
    symbols: tuple[ParsedSymbol, ...] = ()
    chunks: tuple[ParsedChunk, ...] = ()
    # Populated when the file could not be parsed; the file is then kept as a
    # single chunk so its text is still retrievable.
    error: str | None = None


class ParseError(RuntimeError):
    """Raised only for programmer errors; syntax errors are recorded instead."""


# --- token estimate --------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Cheap, stable stand-in for a tokenizer: about four characters per token."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


# --- line helpers ----------------------------------------------------------


def _split_lines(source: str) -> list[str]:
    return source.splitlines()


def _slice(lines: Sequence[str], start: int, end: int) -> str:
    """Return lines ``start..end`` inclusive, 1-based, without a trailing blank."""
    if start < 1:
        start = 1
    if end > len(lines):
        end = len(lines)
    if end < start:
        return ""
    return "\n".join(lines[start - 1 : end])


def _statement_spans_from_lines(lines: Sequence[str], start: int, end: int) -> tuple[tuple[int, int], ...]:
    """Fallback splitter: blank-line separated blocks within a range."""
    spans: list[tuple[int, int]] = []
    block_start = start
    for number in range(start, min(end, len(lines)) + 1):
        if lines[number - 1].strip():
            continue
        if number - 1 >= block_start:
            spans.append((block_start, number - 1))
        block_start = number + 1
    if block_start <= end:
        spans.append((block_start, end))
    return tuple(span for span in spans if span[1] >= span[0])


def _is_config_file(path: str) -> bool:
    name = Path(path).name.lower()
    if name in CONFIG_FILENAMES:
        return True
    return Path(name).suffix in CONFIG_SUFFIXES


def _language_for(path: str, language: str | None) -> str | None:
    if language:
        lowered = language.lower()
        if lowered in (LANGUAGE_PYTHON, LANGUAGE_JAVASCRIPT, LANGUAGE_TYPESCRIPT):
            return lowered
    suffix = Path(path).suffix.lower()
    if suffix in PYTHON_SUFFIXES:
        return LANGUAGE_PYTHON
    if suffix in JAVASCRIPT_SUFFIXES:
        return LANGUAGE_JAVASCRIPT
    if suffix in TYPESCRIPT_SUFFIXES:
        return LANGUAGE_TYPESCRIPT
    return language.lower() if language else None


# --- chunking --------------------------------------------------------------


def chunk_symbol(
    symbol: ParsedSymbol,
    lines: Sequence[str],
    *,
    max_chunk_tokens: int = DEFAULT_MAX_CHUNK_TOKENS,
) -> list[ParsedChunk]:
    """One chunk for a symbol, split on statement boundaries if too large."""
    content = _slice(lines, symbol.line_start, symbol.line_end)
    if not content:
        return []
    if estimate_tokens(content) <= max_chunk_tokens:
        return [
            ParsedChunk(
                line_start=symbol.line_start,
                line_end=symbol.line_end,
                content=content,
                token_count=estimate_tokens(content),
            )
        ]

    spans = symbol.statement_spans or _statement_spans_from_lines(
        lines, symbol.line_start, symbol.line_end
    )
    # A symbol's first statement starts after its header (`def f(a):`, `class C:`,
    # decorators). Keep the header with the first chunk so a split function still
    # opens with its signature.
    if spans and spans[0][0] > symbol.line_start:
        spans = ((symbol.line_start, spans[0][0] - 1), *spans)
    if not spans:
        return [
            ParsedChunk(
                line_start=symbol.line_start,
                line_end=symbol.line_end,
                content=content,
                token_count=estimate_tokens(content),
            )
        ]

    chunks: list[ParsedChunk] = []
    group_start, group_end = spans[0]
    for span_start, span_end in spans[1:]:
        candidate = _slice(lines, group_start, span_end)
        if estimate_tokens(candidate) <= max_chunk_tokens:
            group_end = span_end
            continue
        chunks.append(_make_chunk(lines, group_start, group_end))
        group_start, group_end = span_start, span_end
    chunks.append(_make_chunk(lines, group_start, group_end))
    return [chunk for chunk in chunks if chunk.content]


def _make_chunk(lines: Sequence[str], start: int, end: int) -> ParsedChunk:
    content = _slice(lines, start, end)
    return ParsedChunk(
        line_start=start,
        line_end=end,
        content=content,
        token_count=estimate_tokens(content),
    )


# --- Python (ast) ----------------------------------------------------------


def _call_name(node: ast.AST) -> str | None:
    """Dotted name of a decorator/call target, e.g. ``router.get``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    return None


def _string_arg(node: ast.Call, position: int = 0, keyword: str | None = None) -> str | None:
    if keyword:
        for kw in node.keywords:
            if kw.arg == keyword and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
    if len(node.args) > position:
        arg = node.args[position]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return None


def _string_list_arg(node: ast.Call, keyword: str) -> list[str] | None:
    for kw in node.keywords:
        if kw.arg != keyword or not isinstance(kw.value, (ast.List, ast.Tuple)):
            continue
        values = [
            element.value
            for element in kw.value.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
        if values:
            return values
    return None


def _dependency_name(value: ast.AST) -> str | None:
    """The dependency inside ``Depends(x)`` / ``Security(x)``, else the name itself."""
    if isinstance(value, ast.Call):
        callee = _call_name(value.func) or ""
        if callee.split(".")[-1] in {"Depends", "Security"} and value.args:
            return _call_name(value.args[0])
        return callee or None
    return _call_name(value)


def _auth_markers_from_decorator(node: ast.Call) -> list[str]:
    markers: list[str] = []
    for kw in node.keywords:
        if kw.arg != "dependencies":
            continue
        values = kw.value.elts if isinstance(kw.value, (ast.List, ast.Tuple)) else [kw.value]
        for value in values:
            name = _dependency_name(value)
            if name:
                markers.append(name)
    return markers


def _auth_markers_from_signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    markers: list[str] = []
    args = [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]
    for argument in args:
        name = _call_name(argument.annotation) if argument.annotation else None
        if name and name.split(".")[-1] in {"Depends", "Security"}:
            markers.append(argument.arg)
        elif argument.arg in {"current_user", "user", "principal"}:
            markers.append(argument.arg)
    return markers


def _route_from_decorator(decorator: ast.AST) -> dict | None:
    """Describe the route a decorator declares, or None when it is not one."""
    if not isinstance(decorator, ast.Call):
        return None
    target = decorator.func
    if not isinstance(target, ast.Attribute):
        return None

    attribute = target.attr.lower()
    if attribute not in HTTP_VERBS:
        return None

    framework = "fastapi"
    if attribute == "route":
        # Flask: @app.route("/x", methods=["POST"])
        framework = "flask"
        methods = _string_list_arg(decorator, "methods") or ["GET"]
        path = _string_arg(decorator, 0)
        prefix = None
    else:
        methods = [attribute.upper()]
        path = _string_arg(decorator, 0)
        prefix = None

    receivers = _call_name(target.value)
    if receivers and receivers.split(".")[0] not in ROUTER_OBJECTS:
        # Something like obj.get(...) inside business logic is not a route.
        return None

    return {
        "framework": framework,
        "method": methods[0] if methods else None,
        "methods": [method.upper() for method in methods],
        "path": path,
        "router": receivers,
        "prefix": prefix,
        "auth_markers": _auth_markers_from_decorator(decorator),
    }


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> list[str]:
    names: list[str] = []
    for decorator in node.decorator_list:
        name = _call_name(decorator)
        if name:
            names.append(name)
    return names


def _signature(source: str, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    """The declaration line(s) without the body: ``def foo(a, b) -> int:``."""
    lines = _split_lines(source)
    start = node.lineno
    # The signature ends at the colon that opens the body, i.e. the last line of
    # the arguments/returns, which ast records as body[0].lineno - 1 at most.
    end = start
    if getattr(node, "body", None):
        end = max(start, node.body[0].lineno - 1)
    text = _slice(lines, start, end).strip()
    if text.endswith(":"):
        text = text[:-1]
    return text


def _python_symbol_for_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    source: str,
    *,
    in_class: bool,
) -> ParsedSymbol:
    lines = _split_lines(source)
    routes = [route for route in (_route_from_decorator(d) for d in node.decorator_list) if route]
    decorators = _decorator_names(node)
    auth_markers: list[str] = []
    for route in routes:
        auth_markers.extend(route.get("auth_markers") or [])
    auth_markers.extend(_auth_markers_from_signature(node))
    auth_markers = sorted(set(auth_markers))

    metadata: dict = {
        "decorators": decorators,
        "is_async": isinstance(node, ast.AsyncFunctionDef),
    }
    if routes:
        metadata["route"] = routes[0]
        metadata["framework"] = routes[0]["framework"]
    if auth_markers:
        metadata["auth_markers"] = auth_markers

    kind = SymbolKind.ROUTE if routes else (SymbolKind.METHOD if in_class else SymbolKind.FUNCTION)
    body_spans = tuple((statement.lineno, statement.end_lineno or statement.lineno) for statement in node.body)
    return ParsedSymbol(
        name=node.name,
        kind=kind,
        line_start=node.lineno,
        line_end=node.end_lineno or node.lineno,
        signature=_signature(source, node),
        metadata=metadata,
        statement_spans=body_spans or ((node.lineno, node.end_lineno or node.lineno),),
    )


def _python_symbol_for_class(node: ast.ClassDef, source: str) -> ParsedSymbol:
    metadata = {
        "decorators": _decorator_names(node),
        "bases": [_call_name(base) for base in node.bases if _call_name(base)],
    }
    spans = tuple((statement.lineno, statement.end_lineno or statement.lineno) for statement in node.body)
    return ParsedSymbol(
        name=node.name,
        kind=SymbolKind.CLASS,
        line_start=node.lineno,
        line_end=node.end_lineno or node.lineno,
        signature=_signature(source, node),
        metadata=metadata,
        statement_spans=spans,
    )


def _python_import_symbols(tree: ast.Module, source: str) -> list[ParsedSymbol]:
    lines = _split_lines(source)
    symbols: list[ParsedSymbol] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
            symbols.append(
                ParsedSymbol(
                    name=", ".join(modules),
                    kind=SymbolKind.IMPORT,
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                    signature=_slice(lines, node.lineno, node.end_lineno or node.lineno).strip(),
                    metadata={"modules": modules, "imported_names": [alias.asname or alias.name for alias in node.names]},
                )
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = [alias.name for alias in node.names]
            symbols.append(
                ParsedSymbol(
                    name=module or ".",
                    kind=SymbolKind.IMPORT,
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                    signature=_slice(lines, node.lineno, node.end_lineno or node.lineno).strip(),
                    metadata={
                        "module": module,
                        "level": node.level,
                        "imported_names": names,
                        "resolved": [f"{module}.{name}" if module else name for name in names],
                    },
                )
            )
    symbols.sort(key=lambda symbol: (symbol.line_start, symbol.line_end))
    return symbols


def parse_python(source: str, path: str = "<memory>") -> ParsedFile:
    """Parse one Python file with the standard library ``ast`` module."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return _unparsable(path, LANGUAGE_PYTHON, source, f"SyntaxError: {exc.msg} (line {exc.lineno})")

    symbols: list[ParsedSymbol] = []
    symbols.extend(_python_import_symbols(tree, source))

    def visit(node: ast.AST, *, in_class: bool = False) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                symbols.append(_python_symbol_for_class(child, source))
                visit(child, in_class=True)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(_python_symbol_for_function(child, source, in_class=in_class))
                visit(child, in_class=in_class)
            elif isinstance(child, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                # Routes and helpers are sometimes defined under a guard such as
                # `if TYPE_CHECKING:` or `try/except ImportError`.
                visit(child, in_class=in_class)

    visit(tree)
    symbols.sort(key=lambda symbol: (symbol.line_start, symbol.line_end))
    return _finish(path, LANGUAGE_PYTHON, symbols, source)


# --- JavaScript / TypeScript (Tree-sitter) ---------------------------------

_JAVASCRIPT_LANGUAGE = Language(tree_sitter_javascript.language())
_TYPESCRIPT_LANGUAGE = Language(tree_sitter_typescript.language_typescript())
_TSX_LANGUAGE = Language(tree_sitter_typescript.language_tsx())

_NODE_KINDS_FUNCTION = {
    "function_declaration",
    "generator_function_declaration",
    "function_expression",
    "arrow_function",
}
_NODE_KINDS_CLASS = {"class_declaration", "class", "abstract_class_declaration"}


def _ts_language_for(path: str, language: str) -> Language:
    suffix = Path(path).suffix.lower()
    if language == LANGUAGE_TYPESCRIPT:
        return _TSX_LANGUAGE if suffix in {".tsx", ".jsx"} else _TYPESCRIPT_LANGUAGE
    return _JAVASCRIPT_LANGUAGE


def _node_text(node: Node) -> str:
    return node.text.decode("utf8") if node.text is not None else ""


def _ts_identifier_name(node: Node | None) -> str | None:
    if node is None:
        return None
    if node.type in {"identifier", "property_identifier", "type_identifier", "private_property_identifier"}:
        return _node_text(node)
    for field_name in ("name", "property", "key"):
        child = node.child_by_field_name(field_name)
        if child is not None and child.type in {
            "identifier",
            "property_identifier",
            "type_identifier",
            "string",
            "string_fragment",
        }:
            return _node_text(child).strip("'\"`")
    return None


def _ts_string_value(node: Node | None) -> str | None:
    if node is None:
        return None
    text = _node_text(node).strip()
    if node.type in {"string", "template_string"}:
        return text.strip("'\"`")
    return None


def _ts_directives(node: Node) -> list[str]:
    """Decorator-like prefixes: TS decorators, plus the keywords in the range."""
    decorators: list[str] = []
    cursor = node.parent
    while cursor is not None and cursor.type in {"export_statement", "decorator", "ambient_declaration"}:
        if cursor.type == "decorator":
            decorators.append(_node_text(cursor).lstrip("@").strip())
            cursor = cursor.parent if cursor.parent is not None else None
            continue
        cursor = cursor.parent
    return decorators


def _ts_signature(node: Node) -> str | None:
    """The declaration up to and including the parameter list."""
    params = node.child_by_field_name("parameters")
    if params is not None:
        start = node.start_byte
        text = node.text[: params.end_byte - start].decode("utf8", "replace")
        return " ".join(text.split())
    # Variable declarators: `const name = (a) =>`
    name = _ts_identifier_name(node)
    return name


def _ts_params(node: Node) -> list[str]:
    params = node.child_by_field_name("parameters")
    if params is None:
        return []
    names: list[str] = []
    for child in params.named_children:
        text = _node_text(child).strip()
        if text:
            names.append(text)
    return names


def _ts_body_statements(node: Node) -> tuple[tuple[int, int], ...]:
    body = node.child_by_field_name("body")
    if body is None:
        return ()
    if body.type in {"statement_block", "class_body"}:
        spans = [
            (child.start_point[0] + 1, child.end_point[0] + 1)
            for child in body.named_children
            if child.end_point[0] > child.start_point[0] or _node_text(child).strip()
        ]
        return tuple(spans)
    return ((body.start_point[0] + 1, body.end_point[0] + 1),)


def _ts_route_from_call(node: Node) -> dict | None:
    """Describe ``app.get('/x', middleware, handler)`` as a route, if it is one."""
    if node.type != "call_expression":
        return None
    function = node.child_by_field_name("function")
    if function is None or function.type != "member_expression":
        return None
    prop = function.child_by_field_name("property")
    obj = function.child_by_field_name("object")
    method = _node_text(prop).lower() if prop is not None else ""
    if method not in HTTP_VERBS:
        return None
    receiver = _node_text(obj).split(".")[0] if obj is not None else ""
    if receiver not in ROUTER_OBJECTS and not receiver.lower().endswith("router"):
        return None

    args = node.child_by_field_name("arguments")
    path: str | None = None
    middleware: list[str] = []
    if args is not None and args.named_children:
        path = _ts_string_value(args.named_children[0])
        for argument in args.named_children[1:]:
            if argument.type in _NODE_KINDS_FUNCTION:
                # The handler itself, not middleware.
                continue
            text = _node_text(argument).strip()
            if not text:
                continue
            name = text.split("(", 1)[0].strip()
            if name and name not in {"async", "function", "await"}:
                middleware.append(name)

    markers = sorted(
        {name.rsplit(".", 1)[-1] for name in middleware if EXPRESS_MIDDLEWARE_HINT.search(name)}
    )
    return {
        "framework": "express",
        "method": "GET" if method == "route" else method.upper(),
        "methods": ["GET"] if method == "route" else [method.upper()],
        "path": path,
        "router": _node_text(obj) if obj is not None else None,
        "middleware": middleware,
        "auth_markers": markers,
    }


def _require_call(node: Node) -> str | None:
    """Module name when ``node`` is ``require("mod")`` or ``require("mod").x``."""
    if node.type != "call_expression":
        return None
    function = node.child_by_field_name("function")
    if function is None or _node_text(function) != "require":
        return None
    args = node.child_by_field_name("arguments")
    if args is None or not args.named_children:
        return None
    return _ts_string_value(args.named_children[0])


def _ts_require_symbols(root: Node, lines: Sequence[str]) -> list[ParsedSymbol]:
    """CommonJS imports: ``const x = require("m")`` and ``const {a} = require("m")``."""
    symbols: list[ParsedSymbol] = []
    for child in root.named_children:
        if child.type not in {"lexical_declaration", "variable_declaration"}:
            continue
        for declarator in child.named_children:
            if declarator.type != "variable_declarator":
                continue
            value = declarator.child_by_field_name("value")
            module = _require_call(value) if value is not None else None
            if module is None:
                continue
            pattern = declarator.child_by_field_name("name")
            imported = [_node_text(pattern)] if pattern is not None else []
            if pattern is not None and pattern.type == "object_pattern":
                imported = [
                    _ts_identifier_name(element) or _node_text(element).strip()
                    for element in pattern.named_children
                    if element.type in {"shorthand_property_identifier_pattern", "pair_pattern", "identifier"}
                ]
            symbols.append(
                ParsedSymbol(
                    name=module,
                    kind=SymbolKind.IMPORT,
                    line_start=child.start_point[0] + 1,
                    line_end=child.end_point[0] + 1,
                    signature=_slice(lines, child.start_point[0] + 1, child.end_point[0] + 1).strip(),
                    metadata={
                        "module": module,
                        "imported_names": imported,
                        "style": "commonjs",
                        "resolved": [f"{module}.{name}" if module.startswith(".") else name for name in imported],
                    },
                )
            )
    return symbols


def _ts_import_symbols(root: Node, lines: Sequence[str]) -> list[ParsedSymbol]:
    symbols: list[ParsedSymbol] = []
    for child in root.named_children:
        node = child
        if node.type == "export_statement":
            node = node.child_by_field_name("declaration") or node
        if node.type in {"import_statement", "export_statement"} and _node_text(node).lstrip().startswith(
            ("import", "export")
        ):
            text = " ".join(_node_text(node).split())
            if not text.startswith("import") and " from " not in text:
                continue
            module: str | None = None
            child_source = node.child_by_field_name("source")
            if child_source is not None:
                module = _ts_string_value(child_source)
            symbols.append(
                ParsedSymbol(
                    name=module or text[:60],
                    kind=SymbolKind.IMPORT,
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    signature=_slice(lines, node.start_point[0] + 1, node.end_point[0] + 1).strip(),
                    metadata={"module": module, "statement": text[:200]},
                )
            )
    return symbols


def _ts_collect_symbols(root: Node, lines: Sequence[str]) -> list[ParsedSymbol]:
    symbols: list[ParsedSymbol] = []
    route_calls: list[ParsedSymbol] = []

    def add_function(node: Node, *, name: str | None, kind: SymbolKind, wrapper: Node | None = None) -> None:
        span = wrapper or node
        symbols.append(
            ParsedSymbol(
                name=name or "<anonymous>",
                kind=kind,
                line_start=span.start_point[0] + 1,
                line_end=span.end_point[0] + 1,
                signature=_ts_signature(node),
                metadata={
                    "is_async": _node_text(node).split("(", 1)[0].strip().endswith("async"),
                    "parameters": _ts_params(node),
                    "decorators": _ts_directives(span),
                    **(
                        {"route": route}
                        if (route := _ts_route_from_call(node)) is not None
                        else {}
                    ),
                },
                statement_spans=_ts_body_statements(node)
                or ((span.start_point[0] + 1, span.end_point[0] + 1),),
            )
        )

    def visit(node: Node, *, in_class: bool = False) -> None:
        for child in node.named_children:
            wrapper: Node | None = None
            target = child
            if child.type == "export_statement":
                declaration = child.child_by_field_name("declaration")
                if declaration is None:
                    # `export { a, b }` and `export default x` declare nothing here.
                    continue
                # Keep the `export` line in the symbol's range.
                wrapper, target = child, declaration

            if target.type in {"lexical_declaration", "variable_declaration"}:
                for declarator in target.named_children:
                    if declarator.type != "variable_declarator":
                        continue
                    value = declarator.child_by_field_name("value")
                    name = _ts_identifier_name(declarator)
                    if value is None:
                        continue
                    if value.type in _NODE_KINDS_FUNCTION:
                        route = _ts_route_from_call(value)
                        add_function(
                            value,
                            name=name,
                            kind=SymbolKind.ROUTE if route else SymbolKind.FUNCTION,
                            wrapper=declarator.parent,
                        )
                    elif value.type == "call_expression":
                        route = _ts_route_from_call(value)
                        if route:
                            route_calls.append(
                                ParsedSymbol(
                                    name=name or "<route>",
                                    kind=SymbolKind.ROUTE,
                                    line_start=value.start_point[0] + 1,
                                    line_end=value.end_point[0] + 1,
                                    signature=" ".join(_node_text(value).split())[:200],
                                    metadata={"route": route, "handler": name},
                                    statement_spans=((value.start_point[0] + 1, value.end_point[0] + 1),),
                                )
                            )
                continue

            if target.type in _NODE_KINDS_CLASS:
                symbols.append(
                    ParsedSymbol(
                        name=_ts_identifier_name(target) or "<anonymous class>",
                        kind=SymbolKind.CLASS,
                        line_start=(wrapper or target).start_point[0] + 1,
                        line_end=(wrapper or target).end_point[0] + 1,
                        signature=_ts_signature(target),
                        metadata={"decorators": _ts_directives(wrapper or target)},
                        statement_spans=_ts_body_statements(target),
                    )
                )
                visit(target, in_class=True)
            elif target.type in {
                "function_declaration",
                "generator_function_declaration",
                "method_definition",
                "abstract_method_signature",
                "public_field_definition",
            }:
                value = target.child_by_field_name("value")
                if target.type in {"function_declaration", "generator_function_declaration"}:
                    add_function(
                        target,
                        name=_ts_identifier_name(target),
                        kind=SymbolKind.FUNCTION,
                        wrapper=wrapper,
                    )
                elif target.type == "method_definition" or target.type in {
                    "abstract_method_signature",
                }:
                    add_function(target, name=_ts_identifier_name(target), kind=SymbolKind.METHOD, wrapper=wrapper)
                elif value is not None and value.type in _NODE_KINDS_FUNCTION:
                    # Class field holding an arrow function: `handle = () => {}`
                    add_function(
                        value,
                        name=_ts_identifier_name(target),
                        kind=SymbolKind.METHOD,
                        wrapper=target,
                    )
                visit(target, in_class=in_class)
            elif target.type in _NODE_KINDS_FUNCTION:
                name = _ts_identifier_name(target)
                if name is None and target.parent is not None and target.parent.type in {
                    "arguments",
                    "call_expression",
                }:
                    # An inline callback (`app.listen(port, () => ...)`) is not a
                    # named symbol. When it is a route handler the route symbol
                    # already covers exactly these lines.
                    visit(target, in_class=in_class)
                    continue
                add_function(
                    target,
                    name=name,
                    kind=SymbolKind.METHOD if in_class else SymbolKind.FUNCTION,
                    wrapper=wrapper,
                )
                visit(target, in_class=in_class)
            else:
                if target.type == "call_expression":
                    route = _ts_route_from_call(target)
                    if route:
                        route_calls.append(
                            ParsedSymbol(
                                name=f"{route['method']} {route.get('path') or ''}".strip(),
                                kind=SymbolKind.ROUTE,
                                line_start=target.start_point[0] + 1,
                                line_end=target.end_point[0] + 1,
                                signature=" ".join(_node_text(target).split())[:200],
                                metadata={"route": route},
                                statement_spans=((target.start_point[0] + 1, target.end_point[0] + 1),),
                            )
                        )
                visit(target, in_class=in_class)

    visit(root)
    symbols.extend(route_calls)
    symbols.sort(key=lambda symbol: (symbol.line_start, symbol.line_end))
    return symbols


def parse_javascript(source: str, path: str = "<memory>", language: str = LANGUAGE_JAVASCRIPT) -> ParsedFile:
    """Parse a JavaScript/TypeScript file with a Tree-sitter grammar."""
    parser = Parser()
    parser.language = _ts_language_for(path, language)
    tree = parser.parse(source.encode("utf8"))
    if tree.root_node.has_error:
        return _unparsable(
            path,
            language,
            source,
            f"Tree-sitter reported a syntax error starting at line {tree.root_node.start_point[0] + 1}",
        )
    lines = _split_lines(source)
    symbols = _ts_import_symbols(tree.root_node, lines)
    symbols.extend(_ts_require_symbols(tree.root_node, lines))
    symbols.extend(_ts_collect_symbols(tree.root_node, lines))
    symbols.sort(key=lambda symbol: (symbol.line_start, symbol.line_end))
    return _finish(path, language, symbols, source)


# --- configuration files ---------------------------------------------------


def config_symbol(source: str, path: str) -> ParsedSymbol:
    lines = _split_lines(source)
    metadata: dict = {"config_type": Path(path).suffix.lstrip(".") or Path(path).name.lower()}
    if Path(path).name.lower() == "package.json":
        try:
            manifest = json.loads(source)
        except json.JSONDecodeError:
            manifest = None
        if isinstance(manifest, dict):
            metadata["dependencies"] = sorted(
                {
                    **manifest.get("dependencies", {}),
                    **manifest.get("devDependencies", {}),
                }
            )
    return ParsedSymbol(
        name=Path(path).name,
        kind=SymbolKind.CONFIG,
        line_start=1,
        line_end=max(1, len(lines)),
        signature=f"config file: {Path(path).name}",
        metadata=metadata,
        statement_spans=((1, max(1, len(lines))),),
    )


# --- entry points ----------------------------------------------------------


def _unparsable(path: str, language: str, source: str, error: str) -> ParsedFile:
    """A syntax error must not lose the file: keep it as one file-level chunk."""
    lines = _split_lines(source)
    chunks: tuple[ParsedChunk, ...] = ()
    if source.strip():
        content = _slice(lines, 1, max(1, len(lines)))
        chunks = (
            ParsedChunk(
                line_start=1,
                line_end=max(1, len(lines)),
                content=content,
                token_count=estimate_tokens(content),
            ),
        )
    return ParsedFile(path=path, language=language, chunks=chunks, error=error)


def _finish(path: str, language: str | None, symbols: list[ParsedSymbol], source: str) -> ParsedFile:
    lines = _split_lines(source)
    chunks: list[ParsedChunk] = []
    for index, symbol in enumerate(symbols):
        for chunk in chunk_symbol(symbol, lines):
            chunks.append(
                ParsedChunk(
                    line_start=chunk.line_start,
                    line_end=chunk.line_end,
                    content=chunk.content,
                    token_count=chunk.token_count,
                    symbol_index=index,
                )
            )
    if not chunks and source.strip():
        # A file with no recognised symbols is still retrievable as one chunk.
        chunks = [
            ParsedChunk(
                line_start=1,
                line_end=max(1, len(lines)),
                content=_slice(lines, 1, max(1, len(lines))),
                token_count=estimate_tokens(source),
            )
        ]
    return ParsedFile(path=path, language=language, symbols=tuple(symbols), chunks=tuple(chunks))


def parse_file(
    path: str | Path,
    *,
    language: str | None = None,
    source: str | None = None,
    max_chunk_tokens: int = DEFAULT_MAX_CHUNK_TOKENS,
    display_path: str | None = None,
) -> ParsedFile:
    """Parse one file from disk (or from ``source`` when given).

    Unparsable code is reported in ``ParsedFile.error`` and still yields a file
    level chunk, so a syntax error never loses the file.
    """
    path = Path(path) if not isinstance(path, Path) else path
    display_path = display_path or path.as_posix()
    if source is None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ParsedFile(path=display_path, language=language, error=f"unreadable: {exc.strerror}")
    else:
        text = source

    resolved = _language_for(display_path, language)
    if resolved == LANGUAGE_PYTHON:
        parsed = parse_python(text, display_path)
    elif resolved in (LANGUAGE_JAVASCRIPT, LANGUAGE_TYPESCRIPT):
        parsed = parse_javascript(text, display_path, resolved)
    elif _is_config_file(display_path):
        symbol = config_symbol(text, display_path)
        parsed = _finish(display_path, resolved, [symbol], text)
    else:
        parsed = _finish(display_path, resolved, [], text)

    if max_chunk_tokens == DEFAULT_MAX_CHUNK_TOKENS:
        return parsed
    return _rechunk(parsed, text, max_chunk_tokens)


def _rechunk(parsed: ParsedFile, source: str, max_chunk_tokens: int) -> ParsedFile:
    lines = _split_lines(source)
    chunks: list[ParsedChunk] = []
    for index, symbol in enumerate(parsed.symbols):
        for chunk in chunk_symbol(symbol, lines, max_chunk_tokens=max_chunk_tokens):
            chunks.append(
                ParsedChunk(
                    line_start=chunk.line_start,
                    line_end=chunk.line_end,
                    content=chunk.content,
                    token_count=chunk.token_count,
                    symbol_index=index,
                )
            )
    if not chunks:
        chunks = list(parsed.chunks)
    return ParsedFile(
        path=parsed.path,
        language=parsed.language,
        symbols=parsed.symbols,
        chunks=tuple(chunks),
        error=parsed.error,
    )


def parse_snapshot(
    root: str | Path,
    entries: Iterable[tuple[str, str | None]] | None = None,
    *,
    max_chunk_tokens: int = DEFAULT_MAX_CHUNK_TOKENS,
) -> Iterator[ParsedFile]:
    """Yield a ParsedFile per readable file under ``root``.

    ``entries`` is an optional iterable of ``(relative_path, language)``; when it
    is given only those files are parsed, which is how the indexing service
    restricts parsing to files that survived filtering.
    """
    root = Path(root)
    if entries is None:
        candidates = [
            (path.relative_to(root).as_posix(), None)
            for path in sorted(root.rglob("*"))
            if path.is_file()
        ]
    else:
        candidates = list(entries)

    for relative_path, language in candidates:
        absolute = root / relative_path
        if not absolute.is_file():
            continue
        yield parse_file(
            absolute,
            language=language,
            max_chunk_tokens=max_chunk_tokens,
            display_path=relative_path,
        )
