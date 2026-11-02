"""Step 2.3: chunk embeddings, lexical search and vector search."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.db.enums import AuditStatus, SourceType
from app.db.models import EMBEDDING_DIM, Audit, Chunk, Repository
from app.db.session import get_session_factory
from app.indexing.service import index_audit
from app.intake.filtering import index_snapshot
from app.retrieval.embeddings import (
    EmbeddingError,
    GatewayEmbeddingProvider,
    HashingEmbeddingProvider,
    embed_chunks,
    get_provider,
)
from app.retrieval.search import lexical_search, vector_search

FIXTURES = Path(__file__).resolve().parents[1] / "benchmark" / "repos"


# --- providers -------------------------------------------------------------


def test_hashing_provider_is_deterministic_and_has_the_right_width():
    provider = HashingEmbeddingProvider()

    first = provider.embed(["def get_order(order_id): return order"])
    second = provider.embed(["def get_order(order_id): return order"])
    other = provider.embed(["class Settings: debug = False"])

    assert first == second
    assert len(first[0]) == EMBEDDING_DIM
    assert first != other
    assert abs(sum(value * value for value in first[0]) ** 0.5 - 1.0) < 1e-9


def test_hashing_provider_scores_related_text_higher_than_unrelated():
    provider = HashingEmbeddingProvider()
    query = provider.embed(["calculate the total for the order lines"])[0]
    related = provider.embed(["def calculate_total(lines): return total"])[0]
    unrelated = provider.embed(["def get_connection(): return None"])[0]

    def cosine(left, right):
        return sum(a * b for a, b in zip(left, right))

    assert cosine(query, related) > cosine(query, unrelated)


def test_dimension_mismatch_is_refused_with_a_clear_message():
    with pytest.raises(EmbeddingError) as excinfo:
        HashingEmbeddingProvider(dimensions=768)
    assert "vector(1536)" in str(excinfo.value)


def test_gateway_provider_requires_configuration_and_never_echoes_the_key():
    with pytest.raises(EmbeddingError):
        GatewayEmbeddingProvider(base_url="", api_key="secret-value", model="m")

    provider = GatewayEmbeddingProvider(
        base_url="http://127.0.0.1:9/nowhere", api_key="super-secret-key", model="m"
    )
    with pytest.raises(EmbeddingError) as excinfo:
        provider.embed(["hello"])
    message = str(excinfo.value)
    assert "super-secret-key" not in message
    assert "127.0.0.1" in message or "failed" in message


def test_provider_selection_follows_settings():
    offline = get_provider(Settings(embedding_provider="hashing"))
    assert isinstance(offline, HashingEmbeddingProvider)

    gateway = get_provider(
        Settings(
            embedding_provider="gateway",
            litellm_base_url="http://gateway.invalid",
            litellm_api_key="k",
        )
    )
    assert isinstance(gateway, GatewayEmbeddingProvider)
    assert gateway.dimensions == EMBEDDING_DIM


# --- fixtures --------------------------------------------------------------


@pytest.fixture(scope="module")
def indexed_audit():
    """One indexed + embedded python_app audit, shared by the search tests."""
    root = FIXTURES / "python_app"
    repository_id = audit_id = None
    session = get_session_factory()()
    try:
        repository = Repository(
            name=f"pytest-search-{uuid.uuid4().hex[:8]}",
            source_type=SourceType.ZIP,
            storage_path=str(root),
        )
        session.add(repository)
        session.commit()
        session.refresh(repository)
        repository_id = repository.id

        audit = Audit(repository_id=repository.id, status=AuditStatus.QUEUED)
        session.add(audit)
        session.commit()
        session.refresh(audit)
        audit_id = audit.id

        index_snapshot(session, audit.id, root)
        index_audit(session, audit.id, snapshot_root=root)
        summary = embed_chunks(session, audit.id, provider=HashingEmbeddingProvider(), batch_size=8)

        yield audit.id, summary
    finally:
        if audit_id is not None:
            session.delete(session.get(Audit, audit_id))
        if repository_id is not None:
            session.delete(session.get(Repository, repository_id))
        session.commit()
        session.close()


# --- embeddings ------------------------------------------------------------


def test_every_chunk_gets_a_vector_in_batches(indexed_audit):
    audit_id, summary = indexed_audit

    with get_session_factory()() as session:
        total = session.execute(
            select(func.count()).select_from(Chunk).where(Chunk.audit_id == audit_id)
        ).scalar_one()
        embedded = session.execute(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.audit_id == audit_id, Chunk.embedding.is_not(None))
        ).scalar_one()

        assert summary.chunks == total == embedded
        assert summary.dimensions == EMBEDDING_DIM
        # batch_size=8 over more than 8 chunks means more than one gateway call.
        assert summary.batches == -(-total // 8)
        assert summary.batches > 1

        widths = {
            len(vector)
            for vector in session.execute(
                select(Chunk.embedding).where(Chunk.audit_id == audit_id).limit(5)
            ).scalars()
        }
        assert widths == {EMBEDDING_DIM}


def test_embeddings_are_skipped_when_present_unless_forced(indexed_audit):
    audit_id, _ = indexed_audit

    with get_session_factory()() as session:
        again = embed_chunks(session, audit_id, provider=HashingEmbeddingProvider())
        assert again.chunks == 0
        assert again.batches == 0
        assert again.skipped > 0

        forced = embed_chunks(session, audit_id, provider=HashingEmbeddingProvider(), force=True)
        assert forced.chunks > 0


def test_content_tsv_is_populated_for_full_text_search(indexed_audit):
    audit_id, _ = indexed_audit

    with get_session_factory()() as session:
        missing = session.execute(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.audit_id == audit_id, Chunk.content_tsv.is_(None))
        ).scalar_one()
        assert missing == 0

        hits = session.execute(
            select(func.count())
            .select_from(Chunk)
            .where(
                Chunk.audit_id == audit_id,
                Chunk.content_tsv.op("@@")(func.plainto_tsquery("english", "get_order")),
            )
        ).scalar_one()
        assert hits >= 1


# --- the two required queries ---------------------------------------------


def test_exact_function_name_query_returns_that_function_first(indexed_audit):
    audit_id, _ = indexed_audit

    with get_session_factory()() as session:
        hits = lexical_search(session, audit_id, "get_order")

        assert hits, "expected at least one lexical hit"
        assert hits[0].symbol_name == "get_order"
        assert hits[0].file_path == "app/database.py"
        assert (hits[0].line_start, hits[0].line_end) == (19, 30)
        assert hits[0].matched_by == "lexical"
        assert "def get_order" in hits[0].snippet


def test_exact_name_wins_over_a_chunk_that_only_mentions_the_words(indexed_audit):
    audit_id, _ = indexed_audit

    with get_session_factory()() as session:
        hits = lexical_search(session, audit_id, "search_orders", limit=5)

        assert hits[0].symbol_name == "search_orders"
        assert hits[0].file_path == "app/database.py"


def test_plain_language_query_finds_the_right_function(indexed_audit):
    audit_id, _ = indexed_audit

    with get_session_factory()() as session:
        hits = vector_search(session, audit_id, "calculate the total for order lines", limit=5)

        assert hits, "expected vector hits"
        found = [hit.symbol_name for hit in hits]
        assert "calculate_total" in found[:3], found
        assert hits[0].matched_by == "vector"
        assert 0.0 < hits[0].score <= 1.0


def test_vector_search_ranks_related_code_above_unrelated(indexed_audit):
    audit_id, _ = indexed_audit

    with get_session_factory()() as session:
        hits = vector_search(session, audit_id, "database connection cursor", limit=10)

        names = [hit.symbol_name for hit in hits]
        assert "get_connection" in names[:3], names
        assert hits[0].score >= hits[-1].score



def test_search_is_scoped_to_one_audit(indexed_audit):
    audit_id, _ = indexed_audit
    other_audit = uuid.uuid4()

    with get_session_factory()() as session:
        assert lexical_search(session, other_audit, "get_order") == []
        assert vector_search(session, other_audit, "get_order") == []


def test_empty_query_returns_nothing(indexed_audit):
    audit_id, _ = indexed_audit

    with get_session_factory()() as session:
        assert lexical_search(session, audit_id, "   ") == []
        assert vector_search(session, audit_id, "") == []
