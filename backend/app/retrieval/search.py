"""Lexical and vector search over an audit's chunks (step 2.3).

Both functions answer the same question — "which code matches this?" — over the
same table, and both return a ``SearchHit`` carrying the file, symbol name and
line range so a caller can quote evidence without a second query.

``lexical_search`` uses the GIN-indexed ``chunks.content_tsv``; an exact symbol
name match is ranked above a stemmed text match, so searching for ``get_order``
puts that function first even when other chunks mention the words.

``vector_search`` orders by cosine distance over the HNSW-indexed
``chunks.embedding``. Chunks without a vector are skipped rather than ordered
arbitrarily.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.db.models import Chunk, File as FileModel, Symbol
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


def lexical_search(
    session: Session,
    audit_id: uuid.UUID,
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
) -> list[SearchHit]:
    """Full-text search over the audit's chunks, exact symbol names first."""
    cleaned = (query or "").strip()
    if not cleaned:
        return []

    tsquery = func.plainto_tsquery("english", cleaned)
    rank = func.ts_rank_cd(Chunk.content_tsv, tsquery)
    # `get_order` should beat a chunk that merely contains the words.
    exact_name = func.lower(Symbol.name) == cleaned.lower()

    statement = (
        select(
            Chunk.id,
            FileModel.path,
            Symbol.name,
            Symbol.kind,
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
            )
        )
    return hits


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
            )
        )
    return hits


def hybrid_search(
    session: Session,
    audit_id: uuid.UUID,
    query: str,
    *,
    provider: EmbeddingProvider | None = None,
    limit: int = DEFAULT_LIMIT,
    lexical_weight: float = 0.5,
) -> list[SearchHit]:
    """Merge both rankings by chunk, keeping the best score per chunk.

    Not part of step 2.3's minimum; it exists because the agent steps need one
    call that covers both exact names and paraphrases.
    """
    lexical = lexical_search(session, audit_id, query, limit=limit * 2)
    vector = vector_search(session, audit_id, query, provider=provider, limit=limit * 2)

    merged: dict[uuid.UUID, SearchHit] = {}
    scores: dict[uuid.UUID, float] = {}
    for hit in lexical:
        merged[hit.chunk_id] = hit
        scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + lexical_weight * hit.score
    for hit in vector:
        merged.setdefault(hit.chunk_id, hit)
        scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + (1 - lexical_weight) * hit.score

    ordered = sorted(
        merged.values(), key=lambda hit: (-scores[hit.chunk_id], hit.line_start)
    )[:limit]
    return [
        SearchHit(
            chunk_id=hit.chunk_id,
            file_path=hit.file_path,
            symbol_name=hit.symbol_name,
            symbol_kind=hit.symbol_kind,
            line_start=hit.line_start,
            line_end=hit.line_end,
            score=scores[hit.chunk_id],
            snippet=hit.snippet,
            matched_by="hybrid",
        )
        for hit in ordered
    ]
