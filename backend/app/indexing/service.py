"""Store parsed symbols and chunks for an audit (step 2.1).

The parser is pure; this module is the only place that writes to the database.
Indexing is idempotent: re-running it for the same audit replaces that audit's
symbols and chunks instead of duplicating them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Audit,
    CallEdge,
    Chunk,
    File as FileModel,
    ModuleEdge,
    Repository,
    Symbol,
)
from app.indexing.graph import detect_module_cycles, module_edges, resolve_calls
from app.indexing.parser import DEFAULT_MAX_CHUNK_TOKENS, ParsedFile, parse_file


@dataclass(frozen=True)
class IndexingSummary:
    files_indexed: int
    files_skipped: int
    symbols: int
    chunks: int
    errors: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict:
        return {
            "files_indexed": self.files_indexed,
            "files_skipped": self.files_skipped,
            "symbols": self.symbols,
            "chunks": self.chunks,
            "errors": [{"path": path, "error": error} for path, error in self.errors],
        }


def _snapshot_root(session: Session, audit: Audit) -> Path:
    repository = session.get(Repository, audit.repository_id)
    if repository is None:
        raise ValueError(f"audit {audit.id} has no repository")
    return Path(repository.storage_path)


def index_audit(
    session: Session,
    audit_id: uuid.UUID,
    *,
    snapshot_root: Path | None = None,
    max_chunk_tokens: int = DEFAULT_MAX_CHUNK_TOKENS,
) -> IndexingSummary:
    """Parse every included file of an audit and store symbols plus chunks."""
    audit = session.get(Audit, audit_id)
    if audit is None:
        raise ValueError(f"unknown audit {audit_id}")

    root = snapshot_root or _snapshot_root(session, audit)
    files = list(
        session.execute(
            select(FileModel)
            .where(FileModel.audit_id == audit_id)
            .order_by(FileModel.path)
        ).scalars()
    )

    # Replace, never append: a second run must not duplicate rows.
    session.execute(delete(Chunk).where(Chunk.audit_id == audit_id))
    session.execute(delete(Symbol).where(Symbol.audit_id == audit_id))
    session.flush()

    indexed = 0
    skipped = 0
    symbol_count = 0
    chunk_count = 0
    errors: list[tuple[str, str]] = []

    for file_row in files:
        if file_row.is_excluded:
            skipped += 1
            continue

        parsed = parse_file(
            root / file_row.path,
            language=file_row.language,
            max_chunk_tokens=max_chunk_tokens,
            display_path=file_row.path,
        )
        if parsed.error:
            errors.append((file_row.path, parsed.error))

        symbol_ids: list[uuid.UUID] = []
        for symbol in parsed.symbols:
            row = Symbol(
                file_id=file_row.id,
                audit_id=audit.id,
                name=symbol.name,
                kind=symbol.kind,
                line_start=symbol.line_start,
                line_end=symbol.line_end,
                signature=symbol.signature,
                symbol_metadata=symbol.metadata or None,
            )
            session.add(row)
            session.flush()  # the chunk needs the generated symbol id
            symbol_ids.append(row.id)
            symbol_count += 1

        for chunk in parsed.chunks:
            session.add(
                Chunk(
                    file_id=file_row.id,
                    audit_id=audit.id,
                    symbol_id=(
                        symbol_ids[chunk.symbol_index]
                        if chunk.symbol_index is not None and chunk.symbol_index < len(symbol_ids)
                        else None
                    ),
                    line_start=chunk.line_start,
                    line_end=chunk.line_end,
                    content=chunk.content,
                    content_tsv=func.to_tsvector("english", chunk.content),
                    token_count=chunk.token_count,
                )
            )
            chunk_count += 1
        indexed += 1

    session.commit()
    return IndexingSummary(
        files_indexed=indexed,
        files_skipped=skipped,
        symbols=symbol_count,
        chunks=chunk_count,
        errors=tuple(errors),
    )


@dataclass(frozen=True)
class GraphSummary:
    module_edges: int
    call_edges: int
    resolved_calls: int
    unresolved_calls: int
    cycles: tuple[tuple[str, ...], ...] = ()

    def as_dict(self) -> dict:
        return {
            "module_edges": self.module_edges,
            "call_edges": self.call_edges,
            "resolved_calls": self.resolved_calls,
            "unresolved_calls": self.unresolved_calls,
            "cycles": [list(cycle) for cycle in self.cycles],
        }


def _load_parsed(
    session: Session,
    audit: Audit,
    root: Path,
    files: list[FileModel],
) -> tuple[list[ParsedFile], dict[str, str]]:
    parsed_files: list[ParsedFile] = []
    sources: dict[str, str] = {}
    for file_row in files:
        if file_row.is_excluded:
            continue
        absolute = root / file_row.path
        try:
            source = absolute.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        sources[file_row.path] = source
        parsed_files.append(
            parse_file(
                absolute,
                language=file_row.language,
                source=source,
                display_path=file_row.path,
            )
        )
    return parsed_files, sources


def index_graph(
    session: Session,
    audit_id: uuid.UUID,
    *,
    snapshot_root: Path | None = None,
) -> GraphSummary:
    """Fill ``module_edges`` and ``call_edges`` and report circular imports.

    An audit must be indexed first (``index_audit``) so that the symbols these
    edges point at already exist. Re-running replaces the audit's edges.
    """
    audit = session.get(Audit, audit_id)
    if audit is None:
        raise ValueError(f"unknown audit {audit_id}")
    root = snapshot_root or _snapshot_root(session, audit)

    files = list(
        session.execute(
            select(FileModel).where(FileModel.audit_id == audit_id).order_by(FileModel.path)
        ).scalars()
    )
    parsed_files, sources = _load_parsed(session, audit, root, files)

    file_ids = {file_row.path: file_row.id for file_row in files}
    symbol_ids: dict[tuple[str, str], uuid.UUID] = {}
    for symbol, path in session.execute(
        select(Symbol, FileModel.path)
        .join(FileModel, Symbol.file_id == FileModel.id)
        .where(Symbol.audit_id == audit_id)
    ):
        symbol_ids.setdefault((path, symbol.name), symbol.id)

    session.execute(delete(CallEdge).where(CallEdge.audit_id == audit_id))
    session.execute(delete(ModuleEdge).where(ModuleEdge.audit_id == audit_id))
    session.flush()

    edge_count = 0
    for edge in module_edges(parsed_files):
        from_id = file_ids.get(edge.from_path)
        to_id = file_ids.get(edge.to_path)
        if from_id is None or to_id is None:
            continue
        session.add(
            ModuleEdge(
                audit_id=audit_id,
                from_file_id=from_id,
                to_file_id=to_id,
                kind=edge.kind,
            )
        )
        edge_count += 1

    call_count = 0
    resolved_count = 0
    for call in resolve_calls(parsed_files, sources):
        caller_id = symbol_ids.get((call.caller_path, call.caller_name))
        if caller_id is None:
            continue
        callee_id = symbol_ids.get(call.callee_key) if call.callee_key else None
        if callee_id is not None:
            resolved_count += 1
        session.add(
            CallEdge(
                audit_id=audit_id,
                caller_symbol_id=caller_id,
                callee_symbol_id=callee_id,
                callee_name=call.callee_name,
            )
        )
        call_count += 1

    session.commit()
    cycles = detect_module_cycles(parsed_files)
    return GraphSummary(
        module_edges=edge_count,
        call_edges=call_count,
        resolved_calls=resolved_count,
        unresolved_calls=call_count - resolved_count,
        cycles=tuple(tuple(cycle) for cycle in cycles),
    )
