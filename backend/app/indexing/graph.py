"""Call extraction, import resolution and cycle detection (step 2.2).

This module is pure: it takes the parsed files and their source text and returns
edges. ``app.indexing.service.index_graph`` is what writes them to
``module_edges`` and ``call_edges``.

Import resolution is deliberately conservative. A module string that is not part
of the repository (``express``, ``fastapi``, ``os``) produces no module edge, and
a callee that cannot be tied to exactly one symbol leaves
``callee_symbol_id`` NULL while keeping the name it was called by.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

from tree_sitter import Language, Node, Parser

import tree_sitter_javascript
import tree_sitter_typescript

from app.db.enums import SymbolKind
from app.indexing.parser import (
    JAVASCRIPT_SUFFIXES,
    LANGUAGE_JAVASCRIPT,
    LANGUAGE_TYPESCRIPT,
    PYTHON_SUFFIXES,
    TYPESCRIPT_SUFFIXES,
    ParsedFile,
    ParsedSymbol,
)

# Extensions tried when a bare module string has to be turned into a file.
JS_RESOLUTION_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".json")
JS_INDEX_FILES = (
    "index.js",
    "index.jsx",
    "index.mjs",
    "index.cjs",
    "index.ts",
    "index.tsx",
)

# Names that are language builtins or ubiquitous framework calls; recording them
# as call edges would only add noise.
IGNORED_CALLEE_PREFIXES = frozenset(
    {
        "self",
        "super",
        "cls",
        "print",
        "len",
        "range",
        "str",
        "int",
        "float",
        "dict",
        "list",
        "set",
        "tuple",
        "bool",
        "isinstance",
        "getattr",
        "setattr",
        "hasattr",
        "enumerate",
        "zip",
        "open",
        "sorted",
        "sum",
        "min",
        "max",
        "any",
        "all",
        "console",
        "JSON",
        "Object",
        "Promise",
        "Math",
        "require",
        "module",
        "exports",
    }
)


@dataclass(frozen=True)
class ModuleEdgeData:
    from_path: str
    to_path: str
    kind: str = "import"
    module: str = ""


@dataclass(frozen=True)
class CallData:
    caller_path: str
    caller_name: str
    callee_name: str
    line: int


@dataclass(frozen=True)
class ResolvedCall:
    """A call site after resolution; ``callee_key`` is None when unresolved."""

    caller_path: str
    caller_name: str
    callee_name: str
    callee_key: tuple[str, str] | None  # (file path, symbol name)
    line: int

    @property
    def resolved(self) -> bool:
        return self.callee_key is not None


# --- module identity -------------------------------------------------------


def python_module_name(path: str) -> str:
    """``app/routes/orders.py`` -> ``app.routes.orders``; ``__init__`` drops off."""
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def js_module_name(path: str) -> str:
    """``src/lib/queue.js`` -> ``src/lib/queue``."""
    return str(PurePosixPath(path).with_suffix(""))


def _is_python(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in PYTHON_SUFFIXES


def _is_javascript(path: str) -> bool:
    suffix = PurePosixPath(path).suffix.lower()
    return suffix in JAVASCRIPT_SUFFIXES or suffix in TYPESCRIPT_SUFFIXES


# --- import resolution -----------------------------------------------------


class ModuleResolver:
    """Maps a module string from one file to a file that exists in the repo."""

    def __init__(self, paths: Iterable[str]) -> None:
        self.paths = {str(PurePosixPath(path)) for path in paths}
        self.by_python_module: dict[str, str] = {}
        self.by_package: dict[str, str] = {}
        for path in sorted(self.paths):
            if not _is_python(path):
                continue
            module = python_module_name(path)
            self.by_python_module.setdefault(module, path)
            # `from app.routes import orders` imports the package's module.
            if PurePosixPath(path).name == "__init__.py":
                self.by_package.setdefault(module, path)
            elif PurePosixPath(path).stem == "__init__":
                self.by_package.setdefault(module, path)
        self.js_paths = {path for path in self.paths if _is_javascript(path)}

    # -- python ------------------------------------------------------------

    def resolve_python(self, importer: str, module: str, level: int = 0) -> str | None:
        if level:
            base = PurePosixPath(importer).parent
            for _ in range(level - 1):
                base = base.parent
            target = f"{base.as_posix().replace('/', '.')}.{module}".strip(".")
            if not base.as_posix() or base.as_posix() == ".":
                target = module
        else:
            target = module
        if not target:
            return None
        if target in self.by_python_module:
            return self.by_python_module[target]
        # A package import may name a module inside the package.
        for path in sorted(self.paths):
            if _is_python(path) and python_module_name(path) == target:
                return path
        return None

    # -- javascript --------------------------------------------------------

    def resolve_javascript(self, importer: str, module: str) -> str | None:
        if not module.startswith("."):
            return None  # a package, not a file in this repository
        base = (PurePosixPath(importer).parent / module)
        # Normalise "a/../b" without touching the filesystem.
        parts: list[str] = []
        for part in base.parts:
            if part == "..":
                if parts:
                    parts.pop()
                continue
            if part in (".", ""):
                continue
            parts.append(part)
        candidate = PurePosixPath(*parts).as_posix() if parts else "."

        options = [candidate]
        options.extend(f"{candidate}{suffix}" for suffix in JS_RESOLUTION_SUFFIXES)
        options.extend(f"{candidate}/{index}" for index in JS_INDEX_FILES)
        for option in options:
            if option in self.js_paths or option in self.paths:
                return option
        return None

    def resolve(self, importer: str, module: str, level: int = 0) -> str | None:
        if not module:
            return None
        if _is_python(importer):
            return self.resolve_python(importer, module, level)
        return self.resolve_javascript(importer, module)


def module_edges(parsed_files: Sequence[ParsedFile]) -> list[ModuleEdgeData]:
    """One edge per import that points at a file inside the repository."""
    resolver = ModuleResolver(parsed.path for parsed in parsed_files)
    edges: set[ModuleEdgeData] = set()
    for parsed in parsed_files:
        for symbol in parsed.symbols:
            if symbol.kind is not SymbolKind.IMPORT:
                continue
            module = symbol.metadata.get("module") or ""
            if not module and symbol.metadata.get("modules"):
                for single in symbol.metadata["modules"]:
                    target = resolver.resolve(parsed.path, single)
                    if target and target != parsed.path:
                        edges.add(ModuleEdgeData(parsed.path, target, "import", single))
                continue
            target = resolver.resolve(parsed.path, module, int(symbol.metadata.get("level") or 0))
            if target and target != parsed.path:
                edges.add(ModuleEdgeData(parsed.path, target, "import", module))
    return sorted(edges, key=lambda edge: (edge.from_path, edge.to_path))


# --- call extraction -------------------------------------------------------


def _strip_chain(name: str) -> str:
    """``self.repo.load`` -> ``load``; keeps enough for the name to match."""
    return name.rsplit(".", 1)[-1]


def _python_caller_for(line: int, symbols: Sequence[ParsedSymbol]) -> ParsedSymbol | None:
    best: ParsedSymbol | None = None
    for symbol in symbols:
        if symbol.kind in {SymbolKind.IMPORT, SymbolKind.CONFIG}:
            continue
        if symbol.line_start <= line <= symbol.line_end:
            if best is None or (symbol.line_end - symbol.line_start) < (
                best.line_end - best.line_start
            ):
                best = symbol
    return best


def _python_calls(parsed: ParsedFile, source: str) -> list[CallData]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    calls: list[CallData] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node.func)
        if not name or _strip_chain(name) in IGNORED_CALLEE_PREFIXES:
            continue
        caller = _python_caller_for(node.lineno, parsed.symbols)
        if caller is None:
            continue
        calls.append(
            CallData(
                caller_path=parsed.path,
                caller_name=caller.name,
                callee_name=name,
                line=node.lineno,
            )
        )
    return calls


def _callee_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _callee_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _callee_name(node.func)
    if isinstance(node, ast.Subscript):
        return _callee_name(node.value)
    return None


_JS_LANGUAGE = Language(tree_sitter_javascript.language())
_TS_LANGUAGE = Language(tree_sitter_typescript.language_typescript())
_TSX_LANGUAGE = Language(tree_sitter_typescript.language_tsx())


def _js_parser_for(path: str, language: str) -> Parser:
    parser = Parser()
    if language == LANGUAGE_TYPESCRIPT:
        parser.language = _TSX_LANGUAGE if path.endswith((".tsx", ".jsx")) else _TS_LANGUAGE
    else:
        parser.language = _JS_LANGUAGE
    return parser


def _node_text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf8")


def _js_callee_name(call: Node) -> str | None:
    function = call.child_by_field_name("function")
    if function is None:
        return None
    if function.type == "identifier":
        return _node_text(function)
    if function.type == "member_expression":
        prop = function.child_by_field_name("property")
        obj = function.child_by_field_name("object")
        prop_text = _node_text(prop)
        obj_text = _node_text(obj)
        if not prop_text:
            return None
        return f"{obj_text}.{prop_text}" if obj_text else prop_text
    return _node_text(function) or None


def _js_calls(parsed: ParsedFile, source: str) -> list[CallData]:
    language = parsed.language or LANGUAGE_JAVASCRIPT
    parser = _js_parser_for(parsed.path, language)
    tree = parser.parse(source.encode("utf8"))

    calls: list[CallData] = []
    ranges = [
        symbol
        for symbol in parsed.symbols
        if symbol.kind not in {SymbolKind.IMPORT, SymbolKind.CONFIG, SymbolKind.CLASS}
    ]

    def caller_for(line: int) -> ParsedSymbol | None:
        best: ParsedSymbol | None = None
        for symbol in ranges:
            if symbol.line_start <= line <= symbol.line_end:
                if best is None or (symbol.line_end - symbol.line_start) < (
                    best.line_end - best.line_start
                ):
                    best = symbol
        return best

    def visit(node: Node) -> None:
        if node.type == "call_expression":
            name = _js_callee_name(node)
            caller = caller_for(node.start_point[0] + 1)
            if name and _strip_chain(name) in IGNORED_CALLEE_PREFIXES:
                name = None
            if name and caller is not None:
                calls.append(
                    CallData(
                        caller_path=parsed.path,
                        caller_name=caller.name,
                        callee_name=name,
                        line=node.start_point[0] + 1,
                    )
                )
        for child in node.children:
            visit(child)

    visit(tree.root_node)
    return calls


def extract_calls(parsed: ParsedFile, source: str) -> list[CallData]:
    """Every call made inside a function, method or route of one file."""
    if _is_python(parsed.path):
        return _python_calls(parsed, source)
    if _is_javascript(parsed.path):
        return _js_calls(parsed, source)
    return []


# --- callee resolution -----------------------------------------------------


class SymbolIndex:
    """Lookup tables used to tie a call site to a symbol inside the repository."""

    def __init__(self, parsed_files: Sequence[ParsedFile]) -> None:
        self.by_file: dict[str, dict[str, str]] = {}
        self.by_name: dict[str, list[tuple[str, str]]] = {}
        self.imported_from: dict[str, dict[str, tuple[str, str | None]]] = {}
        self.resolver = ModuleResolver(parsed.path for parsed in parsed_files)

        for parsed in parsed_files:
            local: dict[str, str] = {}
            imports: dict[str, tuple[str, str | None]] = {}
            for symbol in parsed.symbols:
                if symbol.kind is SymbolKind.IMPORT:
                    self._record_import(parsed.path, symbol, imports)
                    continue
                if symbol.kind is SymbolKind.CONFIG:
                    continue
                local.setdefault(symbol.name, symbol.name)
                self.by_name.setdefault(symbol.name, []).append((parsed.path, symbol.name))
            self.by_file[parsed.path] = local
            self.imported_from[parsed.path] = imports

    def _record_import(
        self, importer: str, symbol: ParsedSymbol, imports: dict[str, tuple[str, str | None]]
    ) -> None:
        module = symbol.metadata.get("module") or ""
        level = int(symbol.metadata.get("level") or 0)
        names = symbol.metadata.get("imported_names") or []
        if not names and symbol.metadata.get("modules"):
            for single in symbol.metadata["modules"]:
                imports[single.split(".")[-1]] = (single, None)
            return
        for name in names:
            imports[name] = (module, name if module else None)

    def resolve(self, caller_path: str, callee_name: str) -> tuple[str, str] | None:
        """Return ``(file_path, symbol_name)`` or None when it cannot be pinned down."""
        last = _strip_chain(callee_name)
        if not last or last in IGNORED_CALLEE_PREFIXES:
            return None

        # 1. A symbol in the same file.
        if last in self.by_file.get(caller_path, {}):
            return (caller_path, last)

        # 2. An imported name whose module is a file in the repository.
        module, imported_name = self.imported_from.get(caller_path, {}).get(last, ("", None))
        if module:
            target_file = self.resolver.resolve(caller_path, module)
            if target_file:
                for candidate in (imported_name, last):
                    if candidate and candidate in self.by_file.get(target_file, {}):
                        return (target_file, candidate)

        # 3. A name defined exactly once in the repository.
        candidates = self.by_name.get(last, [])
        unique_paths = {path for path, _ in candidates}
        if len(unique_paths) == 1:
            return candidates[0]
        return None


def resolve_calls(
    parsed_files: Sequence[ParsedFile], sources: dict[str, str]
) -> list[ResolvedCall]:
    """Extract every call and resolve as many callees as the repository allows."""
    index = SymbolIndex(parsed_files)
    resolved: list[ResolvedCall] = []
    for parsed in parsed_files:
        source = sources.get(parsed.path, "")
        for call in extract_calls(parsed, source):
            resolved.append(
                ResolvedCall(
                    caller_path=call.caller_path,
                    caller_name=call.caller_name,
                    callee_name=call.callee_name,
                    callee_key=index.resolve(call.caller_path, call.callee_name),
                    line=call.line,
                )
            )
    return resolved


# --- cycles (Tarjan) -------------------------------------------------------


def strongly_connected_components(
    nodes: Iterable[str], edges: Iterable[tuple[str, str]]
) -> list[list[str]]:
    """Tarjan's algorithm, iteratively, so a deep import chain cannot overflow.

    Components and their members are sorted so repeated runs agree.
    """
    adjacency: dict[str, list[str]] = {node: [] for node in nodes}
    for source, target in edges:
        adjacency.setdefault(source, [])
        adjacency.setdefault(target, [])
        if target not in adjacency[source]:
            adjacency[source].append(target)
    for targets in adjacency.values():
        targets.sort()

    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0

    for root in sorted(adjacency):
        if root in index_of:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, child_index = work.pop()
            if child_index == 0:
                index_of[node] = counter
                lowlink[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)

            targets = adjacency[node]
            recursed = False
            for position in range(child_index, len(targets)):
                target = targets[position]
                if target not in index_of:
                    work.append((node, position + 1))
                    work.append((target, 0))
                    recursed = True
                    break
                if target in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[target])
            if recursed:
                continue

            if lowlink[node] == index_of[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(sorted(component))

            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])

    return sorted(components)


def _order_cycle(component: Sequence[str], edges: set[tuple[str, str]]) -> list[str]:
    """Walk the cycle once so the list reads as a path, not a bag of files."""
    remaining = set(component)
    current = min(component)
    ordered = [current]
    remaining.discard(current)
    while remaining:
        successors = [target for target in sorted(remaining) if (current, target) in edges]
        if not successors:
            break
        current = successors[0]
        ordered.append(current)
        remaining.discard(current)
    ordered.extend(sorted(remaining))
    return ordered


def find_cycles(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> list[list[str]]:
    """Every circular dependency, as an ordered list of file paths.

    A component counts as a cycle when it has more than one member, or one member
    that imports itself. Self-imports are reported as a one-item cycle.
    """
    edge_list = list(edges)
    edge_set = set(edge_list)
    cycles: list[list[str]] = []
    for component in strongly_connected_components(nodes, edge_list):
        if len(component) > 1:
            cycles.append(_order_cycle(component, edge_set))
        elif (component[0], component[0]) in edge_set:
            cycles.append([component[0]])
    return cycles


def detect_module_cycles(parsed_files: Sequence[ParsedFile]) -> list[list[str]]:
    """Circular dependencies between repository files, from their imports."""
    edges = module_edges(parsed_files)
    return find_cycles(
        [parsed.path for parsed in parsed_files],
        [(edge.from_path, edge.to_path) for edge in edges],
    )
