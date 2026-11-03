"""Retrieval over an audit's chunks (steps 2.3 and 2.4).

Three primitives answer the same question — "which code matches this?" — over
the same tables, and all return a ``SearchHit`` carrying the file, symbol name
and line range so a caller can quote evidence without a second query.

``lexical_search`` uses the GIN-indexed ``chunks.content_tsv``; an exact symbol
name match is ranked above a stemmed text match, so searching for ``get_order``
puts that function first even when other chunks mention the words.

``vector_search`` orders by cosine distance over the HNSW-indexed
``chunks.embedding``. Chunks without a vector are skipped rather than ordered
arbitrarily.

``symbol_lookup`` matches a query token against symbol names, which is how a
name introduced in conversation (``get_order``) finds its definition even when
the wording around it differs.

``retrieve_context`` fuses all three with reciprocal rank fusion, then widens
the result set along the call graph and repository metadata: callers and
callees, the other routes on the same file, auth helpers, database access,
tests and config. Each returned item carries a reason string naming the
relation that pulled it in, so an agent can tell a query match from a
neighbour. ``naive_top_k`` is the vector-only baseline it is measured against.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.enums import SymbolKind
from app.db.models import CallEdge, Chunk, File as FileModel, Symbol
from app.retrieval.embeddings import EmbeddingProvider, get_provider

DEFAULT_LIMIT = 10
SNIPPET_CHARS = 400


@dataclass(frozen=True)
class SearchHit:
    chunk_id: uuid.UUID
    file_path: str
    symbol_name: str | None
    symbol_kind: str | None
    line_start: int
    line_end: int
    score: float
    snippet: str
    matched_by: str
    symbol_id: uuid.UUID | None = None

    def as_dict(self) -> dict:
        return {
            "chunk_id": str(self.chunk_id),
            "file_path": self.file_path,
            "symbol": self.symbol_name,
            "symbol_kind": self.symbol_kind,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "score": round(self.score, 6),
            "snippet": self.snippet,
            "matched_by": self.matched_by,
        }


def _snippet(content: str) -> str:
    collapsed = " ".join(content.split())
    if len(collapsed) <= SNIPPET_CHARS:
        return collapsed
    return collapsed[:SNIPPET_CHARS].rstrip() + "…"


def _ts_hits(
    session: Session,
    audit_id: uuid.UUID,
    cleaned: str,
    tsquery,
    limit: int,
) -> list[SearchHit]:
    """Rank the chunks matching one tsquery, exact symbol name first."""
    rank = func.ts_rank_cd(Chunk.content_tsv, tsquery)
    # `get_order` should beat a chunk that merely contains the words.
    exact_name = func.lower(Symbol.name) == cleaned.lower()

    statement = (
        select(
            Chunk.id,
            FileModel.path,
            Symbol.name,
            Symbol.kind,
            Symbol.id.label("symbol_id"),
            Chunk.line_start,
            Chunk.line_end,
            Chunk.content,
            rank.label("rank"),
            exact_name.label("exact_name"),
        )
        .join(FileModel, Chunk.file_id == FileModel.id)
        .outerjoin(Symbol, Chunk.symbol_id == Symbol.id)
        .where(
            Chunk.audit_id == audit_id,
            or_(Chunk.content_tsv.op("@@")(tsquery), exact_name),
        )
        .order_by(exact_name.desc(), rank.desc(), Chunk.line_start.asc())
        .limit(max(1, limit))
    )

    hits: list[SearchHit] = []
    for row in session.execute(statement):
        score = float(row.rank or 0.0) + (1.0 if row.exact_name else 0.0)
        hits.append(
            SearchHit(
                chunk_id=row.id,
                file_path=row.path,
                symbol_name=row.name,
                symbol_kind=row.kind.value if hasattr(row.kind, "value") else row.kind,
                line_start=row.line_start,
                line_end=row.line_end,
                score=score,
                snippet=_snippet(row.content),
                matched_by="lexical",
                symbol_id=row.symbol_id,
            )
        )
    return hits


def lexical_search(
    session: Session,
    audit_id: uuid.UUID,
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
) -> list[SearchHit]:
    """Full-text search over the audit's chunks, exact symbol names first.

    ``plainto_tsquery`` requires every term to be present, which is right for a
    symbol or a keyword but returns nothing for a natural-language question
    (\"…without checking who is logged in\" mentions words no single chunk
    contains). When that strict query matches nothing, the search falls back to
    matching any term so the lexical arm still contributes a ranking.
    """
    cleaned = (query or "").strip()
    if not cleaned:
        return []

    hits = _ts_hits(
        session, audit_id, cleaned, func.plainto_tsquery("english", cleaned), limit
    )
    if hits:
        return hits

    terms = re.findall(r"[A-Za-z0-9_]+", cleaned)
    if len(terms) < 2:
        return hits
    # websearch_to_tsquery parses "or" and never raises on stopwords or
    # punctuation the way to_tsquery would on an untrusted string.
    relaxed = func.websearch_to_tsquery("english", " or ".join(terms))
    return _ts_hits(session, audit_id, cleaned, relaxed, limit)


def vector_search(
    session: Session,
    audit_id: uuid.UUID,
    query: str,
    *,
    provider: EmbeddingProvider | None = None,
    limit: int = DEFAULT_LIMIT,
    min_score: float = 0.0,
) -> list[SearchHit]:
    """Nearest chunks by cosine distance over the HNSW-indexed embeddings."""
    cleaned = (query or "").strip()
    if not cleaned:
        return []

    provider = provider or get_provider()
    vector = provider.embed([cleaned])[0]

    distance = Chunk.embedding.cosine_distance(vector)
    statement = (
        select(
            Chunk.id,
            FileModel.path,
            Symbol.name,
            Symbol.kind,
            Symbol.id.label("symbol_id"),
            Chunk.line_start,
            Chunk.line_end,
            Chunk.content,
            (1 - distance).label("similarity"),
        )
        .join(FileModel, Chunk.file_id == FileModel.id)
        .outerjoin(Symbol, Chunk.symbol_id == Symbol.id)
        .where(Chunk.audit_id == audit_id, Chunk.embedding.is_not(None))
        .order_by(distance.asc(), Chunk.line_start.asc())
        .limit(max(1, limit))
    )

    hits: list[SearchHit] = []
    for row in session.execute(statement):
        similarity = float(row.similarity or 0.0)
        if similarity < min_score:
            continue
        hits.append(
            SearchHit(
                chunk_id=row.id,
                file_path=row.path,
                symbol_name=row.name,
                symbol_kind=row.kind.value if hasattr(row.kind, "value") else row.kind,
                line_start=row.line_start,
                line_end=row.line_end,
                score=similarity,
                snippet=_snippet(row.content),
                matched_by="vector",
                symbol_id=row.symbol_id,
            )
        )
    return hits


def symbol_lookup(
    session: Session,
    audit_id: uuid.UUID,
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
) -> list[SearchHit]:
    """Chunks whose symbol is named in the query (``"where is get_order called"``)."""
    cleaned = (query or "").strip()
    if not cleaned:
        return []

    tokens = sorted(
        {
            token.lower()
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", cleaned)
            if len(token) >= MIN_SYMBOL_TOKEN
        }
    )
    if not tokens:
        return []

    name = func.lower(Symbol.name)
    named_in_query = name.in_(tokens)
    substring_in_query = func.strpos(cleaned.lower(), name) > 0

    statement = (
        select(*_chunk_columns())
        .join(FileModel, Chunk.file_id == FileModel.id)
        .join(Symbol, Chunk.symbol_id == Symbol.id)
        .where(
            Chunk.audit_id == audit_id,
            or_(named_in_query, substring_in_query),
        )
        .order_by(named_in_query.desc(), func.length(Symbol.name).asc(), Chunk.line_start.asc())
        .limit(max(1, limit))
    )

    hits: list[SearchHit] = []
    seen: set[uuid.UUID] = set()
    for row in session.execute(statement):
        if row.symbol_id in seen:
            continue
        seen.add(row.symbol_id)
        hits.append(_hit(row, score=1.0, matched_by="symbol"))
    return hits


# --- context retrieval (step 2.4) ------------------------------------------

# Reciprocal rank fusion constant from the original RRF paper (Cormack et al.,
# 2009). It damps the top of each list so one confident ranker cannot bury the
# other two.
RRF_K = 60
# A neighbour inherits this much of its parent's fused score. Below 1, so a
# neighbour never outranks the symbol that pulled it in; high enough that
# closely related code lands beside its parent rather than after every direct
# hit.
CALL_GRAPH_WEIGHT = 0.9
METADATA_WEIGHT = 0.7
MAX_SEEDS = 5
MAX_PER_CATEGORY = 2
MIN_SYMBOL_TOKEN = 3

DATABASE_PATH_PATTERN = re.compile(
    r"(^|/)(db|database|databases|models?|repositories|repository|repo|dao|persistence|store)(/|\.|$)",
    re.IGNORECASE,
)
DATABASE_CONTENT_PATTERN = re.compile(
    r"\b(SELECT|INSERT INTO|UPDATE|DELETE FROM)\b", re.IGNORECASE
)
TEST_PATH_PATTERN = re.compile(
    r"(^|/)(tests?|__tests__|spec)/"
    r"|(^|/)test_[^/]*\.py$"
    r"|_test\.(py|js|jsx|ts|tsx)$"
    r"|\.(test|spec)\.(js|jsx|ts|tsx)$",
    re.IGNORECASE,
)
# The same vocabulary the parser uses for auth markers, kept here because the
# metadata queries run in SQL. Kept in step with app.indexing.parser.
AUTH_SQL_PATTERN = (
    "auth|token|jwt|login|session|permission|role|guard|verify|current_user|principal"
)
DATABASE_SQL_PATTERN = "(^|/)(db|database|databases|models?|repositories|repository|repo|dao|persistence|store)(/|\\.|$)"
DATABASE_SQL_CONTENT = "\\y(SELECT|INSERT INTO|UPDATE|DELETE FROM)\\y"

CONFIG_FILE_NAMES = frozenset(
    {
        ".env.example",
        "alembic.ini",
        "docker-compose.yml",
        "docker-compose.yaml",
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "setup.cfg",
        "tsconfig.json",
    }
)


@dataclass(frozen=True)
class ContextItem:
    """One retrieval result: where it is, how strongly it scored, and why."""

    chunk_id: uuid.UUID
    file_path: str
    symbol_name: str | None
    symbol_kind: str | None
    line_start: int
    line_end: int
    score: float
    reason: str
    snippet: str
    symbol_id: uuid.UUID | None = None

    def as_dict(self) -> dict:
        return {
            "chunk_id": str(self.chunk_id),
            "file_path": self.file_path,
            "symbol": self.symbol_name,
            "symbol_kind": self.symbol_kind,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "score": round(self.score, 6),
            "reason": self.reason,
            "snippet": self.snippet,
        }


def _chunk_columns() -> tuple:
    """The column set every chunk query in this module selects."""
    return (
        Chunk.id,
        FileModel.path,
        Symbol.name,
        Symbol.kind,
        Symbol.id.label("symbol_id"),
        Chunk.line_start,
        Chunk.line_end,
        Chunk.content,
    )


def _hit(row, *, score: float, matched_by: str) -> SearchHit:
    return SearchHit(
        chunk_id=row.id,
        file_path=row.path,
        symbol_name=row.name,
        symbol_kind=row.kind.value if hasattr(row.kind, "value") else row.kind,
        line_start=row.line_start,
        line_end=row.line_end,
        score=score,
        snippet=_snippet(row.content),
        matched_by=matched_by,
        symbol_id=row.symbol_id,
    )


def _chunks_for_symbols(
    session: Session, audit_id: uuid.UUID, symbol_ids: set[uuid.UUID]
) -> dict[uuid.UUID, SearchHit]:
    """One chunk per symbol; a split symbol keeps its first chunk."""
    if not symbol_ids:
        return {}
    statement = (
        select(*_chunk_columns())
        .join(FileModel, Chunk.file_id == FileModel.id)
        .join(Symbol, Chunk.symbol_id == Symbol.id)
        .where(Chunk.audit_id == audit_id, Symbol.id.in_(sorted(symbol_ids)))
        .order_by(Chunk.line_start.asc())
    )
    chunks: dict[uuid.UUID, SearchHit] = {}
    for row in session.execute(statement):
        chunks.setdefault(row.symbol_id, _hit(row, score=0.0, matched_by="context"))
    return chunks


def _call_graph_neighbours(
    session: Session,
    audit_id: uuid.UUID,
    seed_symbol_ids: set[uuid.UUID],
) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
    """Depth-1 callers and callees as ``(neighbour, parent, direction)``."""
    if not seed_symbol_ids:
        return []
    ids = sorted(seed_symbol_ids)

    callers = session.execute(
        select(Symbol.id, CallEdge.callee_symbol_id)
        .join(CallEdge, CallEdge.caller_symbol_id == Symbol.id)
        .where(CallEdge.audit_id == audit_id, CallEdge.callee_symbol_id.in_(ids))
    )
    callees = session.execute(
        select(Symbol.id, CallEdge.caller_symbol_id)
        .join(CallEdge, CallEdge.callee_symbol_id == Symbol.id)
        .where(CallEdge.audit_id == audit_id, CallEdge.caller_symbol_id.in_(ids))
    )

    edges: list[tuple[uuid.UUID, uuid.UUID, str]] = []
    for neighbour_id, parent_id in callers:
        if neighbour_id != parent_id:
            edges.append((neighbour_id, parent_id, "caller"))
    for neighbour_id, parent_id in callees:
        if neighbour_id != parent_id:
            edges.append((neighbour_id, parent_id, "callee"))
    return edges


def _sibling_routes(
    session: Session, audit_id: uuid.UUID, seed_symbol_ids: set[uuid.UUID]
) -> list[SearchHit]:
    """Other routes declared in the same file as a retrieved route."""
    if not seed_symbol_ids:
        return []
    file_ids = {
        file_id
        for (file_id,) in session.execute(
            select(Symbol.file_id).where(
                Symbol.audit_id == audit_id,
                Symbol.id.in_(sorted(seed_symbol_ids)),
                Symbol.kind == SymbolKind.ROUTE,
            )
        )
    }
    if not file_ids:
        return []

    routes = list(
        session.execute(
            select(*_chunk_columns())
            .join(FileModel, Chunk.file_id == FileModel.id)
            .join(Symbol, Chunk.symbol_id == Symbol.id)
            .where(
                Chunk.audit_id == audit_id,
                Symbol.file_id.in_(sorted(file_ids)),
                Symbol.kind == SymbolKind.ROUTE,
                Symbol.id.not_in(sorted(seed_symbol_ids)),
            )
            .order_by(Chunk.line_start.asc())
            .limit(MAX_PER_CATEGORY)
        )
    )
    return [_hit(row, score=0.0, matched_by="metadata") for row in routes]


def _is_database_access(file_path: str, content: str) -> bool:
    """True when code runs SQL or lives in a data-access module."""
    return bool(
        DATABASE_PATH_PATTERN.search(file_path) or DATABASE_CONTENT_PATTERN.search(content)
    )


def _auth_helpers(session: Session, audit_id: uuid.UUID) -> list[SearchHit]:
    """Functions that look like authentication or authorization middleware.

    Nothing is excluded: an auth helper that already arrived as a query hit is
    still the middleware a route can omit, so it keeps the category's credit
    and gains the reason that says what it is.
    """
    rows = session.execute(
        select(*_chunk_columns())
        .join(FileModel, Chunk.file_id == FileModel.id)
        .join(Symbol, Chunk.symbol_id == Symbol.id)
        .where(
            Chunk.audit_id == audit_id,
            Symbol.kind.in_([SymbolKind.FUNCTION, SymbolKind.METHOD]),
            or_(
                Symbol.name.op("~*")(AUTH_SQL_PATTERN),
                FileModel.path.op("~*")(AUTH_SQL_PATTERN),
            ),
        )
        .order_by(Chunk.line_start.asc())
        .limit(MAX_PER_CATEGORY)
    )
    return [_hit(row, score=0.0, matched_by="metadata") for row in rows]


def _database_access(
    session: Session, audit_id: uuid.UUID, exclude: set[uuid.UUID | None]
) -> list[SearchHit]:
    """Symbols that run SQL or live in a data-access module."""
    rows = session.execute(
        select(*_chunk_columns())
        .join(FileModel, Chunk.file_id == FileModel.id)
        .join(Symbol, Chunk.symbol_id == Symbol.id)
        .where(
            Chunk.audit_id == audit_id,
            or_(
                FileModel.path.op("~*")(DATABASE_SQL_PATTERN),
                Chunk.content.op("~*")(DATABASE_SQL_CONTENT),
            ),
        )
        .order_by(Chunk.line_start.asc())
        .limit(MAX_PER_CATEGORY * 3)
    )
    return [
        _hit(row, score=0.0, matched_by="metadata")
        for row in rows
        if row.symbol_id not in exclude
    ][:MAX_PER_CATEGORY]


def _tests_mentioning(
    session: Session, audit_id: uuid.UUID, names: list[str]
) -> list[tuple[SearchHit, str]]:
    """Test files whose content names one of the retrieved symbols."""
    found: list[tuple[SearchHit, str]] = []
    for name in names:
        if len(found) >= MAX_PER_CATEGORY or len(name) < MIN_SYMBOL_TOKEN:
            continue
        rows = session.execute(
            select(*_chunk_columns())
            .join(FileModel, Chunk.file_id == FileModel.id)
            .outerjoin(Symbol, Chunk.symbol_id == Symbol.id)
            .where(Chunk.audit_id == audit_id, Chunk.content.ilike(f"%{name}%"))
            .order_by(Chunk.line_start.asc())
        )
        for row in rows:
            if len(found) >= MAX_PER_CATEGORY:
                break
            if TEST_PATH_PATTERN.search(row.path):
                found.append((_hit(row, score=0.0, matched_by="metadata"), name))
    return found


def _config_files(session: Session, audit_id: uuid.UUID) -> list[SearchHit]:
    """Configuration and manifest chunks, which carry hosts, keys and versions."""
    rows = session.execute(
        select(*_chunk_columns())
        .join(FileModel, Chunk.file_id == FileModel.id)
        .outerjoin(Symbol, Chunk.symbol_id == Symbol.id)
        .where(
            Chunk.audit_id == audit_id,
            or_(
                Symbol.kind == SymbolKind.CONFIG,
                FileModel.path.in_(sorted(CONFIG_FILE_NAMES)),
                FileModel.path.ilike("%config%"),
            ),
        )
        .order_by(Chunk.line_start.asc())
        .limit(MAX_PER_CATEGORY)
    )
    return [_hit(row, score=0.0, matched_by="metadata") for row in rows]


def retrieve_context(
    session: Session,
    audit_id: uuid.UUID,
    query: str,
    *,
    focus_symbol: str | None = None,
    k: int = 10,
    provider: EmbeddingProvider | None = None,
) -> list[ContextItem]:
    """Fused lexical, semantic and symbol retrieval, widened by graph and metadata.

    Reciprocal rank fusion decides the seeds; each seed then pulls in its depth-1
    callers and callees, the sibling routes in its file, auth helpers, database
    access, tests that name it, and configuration. A neighbour's score is a
    fraction of its parent's, so it sorts next to the code that led to it rather
    than replacing it.
    """
    cleaned = (query or "").strip()
    if not cleaned and not focus_symbol:
        return []

    candidate_limit = max(k * 3, 30)
    entries: dict[uuid.UUID, dict] = {}

    def add(hit: SearchHit, delta: float, reason: str) -> None:
        """Credit one chunk; a chunk found twice keeps both reasons."""
        entry = entries.get(hit.chunk_id)
        if entry is None:
            entry = {"hit": hit, "score": 0.0, "reasons": []}
            entries[hit.chunk_id] = entry
        entry["score"] += delta
        if reason not in entry["reasons"]:
            entry["reasons"].append(reason)

    for rank, hit in enumerate(lexical_search(session, audit_id, cleaned, limit=candidate_limit), 1):
        add(hit, 1.0 / (RRF_K + rank), "lexical match")
    for rank, hit in enumerate(
        vector_search(session, audit_id, cleaned, provider=provider, limit=candidate_limit), 1
    ):
        add(hit, 1.0 / (RRF_K + rank), "semantic match")
    for rank, hit in enumerate(symbol_lookup(session, audit_id, cleaned, limit=candidate_limit), 1):
        add(hit, 1.0 / (RRF_K + rank), "symbol name match")

    focus: SearchHit | None = None
    if focus_symbol:
        found = symbol_lookup(session, audit_id, focus_symbol, limit=1)
        focus = found[0] if found else None
        if focus is not None:
            # A named symbol is a stronger statement of intent than any query
            # match, so it is credited as if it had ranked first everywhere.
            add(focus, 3.0 / RRF_K, "focus symbol")

    if not entries:
        return []

    ranked = sorted(
        entries.values(), key=lambda entry: (-entry["score"], entry["hit"].line_start)
    )
    seeds = ranked[:MAX_SEEDS]
    seed_scores = {
        entry["hit"].symbol_id: entry["score"]
        for entry in seeds
        if entry["hit"].symbol_id is not None
    }
    if focus is not None and focus.symbol_id is not None:
        seed_scores.setdefault(focus.symbol_id, entries[focus.chunk_id]["score"])

    seed_names = {
        entry["hit"].symbol_id: entry["hit"].symbol_name
        for entry in seeds
        if entry["hit"].symbol_id is not None
    }

    if seed_scores:
        symbol_ids = set(seed_scores)
        best_seed = max(seed_scores.values())
        neighbour_edges = _call_graph_neighbours(session, audit_id, symbol_ids)
        chunks = _chunks_for_symbols(
            session, audit_id, {neighbour_id for neighbour_id, _, _ in neighbour_edges}
        )
        for neighbour_id, parent_id, direction in neighbour_edges:
            neighbour = chunks.get(neighbour_id)
            if neighbour is None:
                continue
            parent_name = seed_names.get(parent_id) or "the retrieved symbol"
            parent_score = seed_scores.get(parent_id, best_seed)
            if direction == "caller":
                reason = f"caller of {parent_name}"
            elif _is_database_access(neighbour.file_path, neighbour.snippet):
                reason = f"database access called by {parent_name}"
            else:
                reason = f"callee of {parent_name}"
            add(neighbour, parent_score * CALL_GRAPH_WEIGHT, reason)

        for route in _sibling_routes(session, audit_id, symbol_ids):
            add(route, best_seed * METADATA_WEIGHT, f"another route in {route.file_path}")

        already_present = {entry["hit"].symbol_id for entry in entries.values()}
        for helper in _auth_helpers(session, audit_id):
            add(helper, best_seed * METADATA_WEIGHT, "auth middleware")

        for db_chunk in _database_access(session, audit_id, exclude=already_present):
            add(db_chunk, best_seed * METADATA_WEIGHT * 0.9, "database access")

        names = [name for name in seed_names.values() if name]
        for test_chunk, name in _tests_mentioning(session, audit_id, names):
            add(test_chunk, best_seed * METADATA_WEIGHT * 0.8, f"test mentioning {name}")

        for config_chunk in _config_files(session, audit_id):
            add(config_chunk, best_seed * METADATA_WEIGHT * 0.6, "config file")

    final = sorted(
        entries.values(), key=lambda entry: (-entry["score"], entry["hit"].file_path)
    )[: max(1, k)]
    return [
        ContextItem(
            chunk_id=entry["hit"].chunk_id,
            file_path=entry["hit"].file_path,
            symbol_name=entry["hit"].symbol_name,
            symbol_kind=entry["hit"].symbol_kind,
            line_start=entry["hit"].line_start,
            line_end=entry["hit"].line_end,
            score=entry["score"],
            reason=" + ".join(entry["reasons"]),
            snippet=entry["hit"].snippet,
            symbol_id=entry["hit"].symbol_id,
        )
        for entry in final
    ]


def naive_top_k(
    session: Session,
    audit_id: uuid.UUID,
    query: str,
    *,
    k: int = 5,
    provider: EmbeddingProvider | None = None,
) -> list[SearchHit]:
    """Baseline for comparison: plain vector search, no fusion and no expansion."""
    return vector_search(session, audit_id, query, provider=provider, limit=k)
