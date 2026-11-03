"""Step 2.4: fused retrieval widened by the call graph and repository metadata."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.db.enums import AuditStatus, SourceType
from app.db.models import Audit, Repository
from app.db.session import get_session_factory
from app.indexing.service import index_audit, index_graph
from app.intake.filtering import index_snapshot
from app.retrieval.embeddings import HashingEmbeddingProvider, embed_chunks
from app.retrieval.search import naive_top_k, retrieve_context

FIXTURES = Path(__file__).resolve().parents[1] / "benchmark" / "repos"

# The question an auditor would actually ask about benchmark/expected.json
# py-authz-01, deliberately phrased without naming the file or the function.
AUTHZ_QUESTION = (
    "an order endpoint that returns somebody's order by id without checking who is logged in"
)

# The three pieces the issue spans, plus the settings the data access reads.
READ_ORDER = ("app/routes/orders.py", "read_order")
GET_CURRENT_USER = ("app/auth.py", "get_current_user")
GET_ORDER = ("app/database.py", "get_order")
SETTINGS = ("app/config.py", "Settings")


@pytest.fixture(scope="module")
def indexed_audit():
    """One python_app audit with symbols, chunks, vectors and call edges."""
    root = FIXTURES / "python_app"
    repository_id = audit_id = None
    session = get_session_factory()()
    try:
        repository = Repository(
            name=f"pytest-context-{uuid.uuid4().hex[:8]}",
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
        # retrieve_context widens along call edges, so the graph must exist.
        index_graph(session, audit.id, snapshot_root=root)
        embed_chunks(session, audit.id, provider=HashingEmbeddingProvider(), batch_size=8)

        yield audit.id
    finally:
        if audit_id is not None:
            session.delete(session.get(Audit, audit_id))
        if repository_id is not None:
            session.delete(session.get(Repository, repository_id))
        session.commit()
        session.close()


def located(items):
    """Every (file, symbol) an item set covers."""
    return {(item.file_path, item.symbol_name) for item in items}


# --- the planted issue -----------------------------------------------------


def test_the_unprotected_route_comes_back_with_the_middleware_it_omits(indexed_audit):
    """Route, handler, auth middleware and the DB call arrive together.

    This is the step's acceptance criterion: reading any one of these alone
    hides the bug, because the gap is between the route and a helper it never
    calls.
    """
    with get_session_factory()() as session:
        items = retrieve_context(session, indexed_audit, AUTHZ_QUESTION, k=10)

    found = located(items)
    assert READ_ORDER in found, found
    assert GET_CURRENT_USER in found, found
    assert GET_ORDER in found, found


def test_the_auth_helper_is_labelled_as_such(indexed_audit):
    with get_session_factory()() as session:
        items = retrieve_context(session, indexed_audit, AUTHZ_QUESTION, k=10)

    helper = next(item for item in items if (item.file_path, item.symbol_name) == GET_CURRENT_USER)
    assert "auth middleware" in helper.reason
    # The helper is a neighbour of nothing: it is related by metadata, not by a
    # call from the route, which is exactly the finding.
    assert "caller of" not in helper.reason
    assert "callee of" not in helper.reason


def test_the_database_call_is_reached_through_the_route(indexed_audit):
    with get_session_factory()() as session:
        items = retrieve_context(session, indexed_audit, AUTHZ_QUESTION, k=10)

    relation = next(item for item in items if (item.file_path, item.symbol_name) == GET_ORDER)
    assert "database access called by read_order" in relation.reason


def test_the_settings_the_data_access_reads_are_included(indexed_audit):
    with get_session_factory()() as session:
        items = retrieve_context(session, indexed_audit, AUTHZ_QUESTION, k=10)

    assert SETTINGS in located(items)


# --- shape of the result ---------------------------------------------------


def test_every_item_has_a_location_a_score_and_a_reason(indexed_audit):
    with get_session_factory()() as session:
        items = retrieve_context(session, indexed_audit, AUTHZ_QUESTION, k=10)

    assert items
    for item in items:
        assert item.file_path
        assert 0 < item.line_start <= item.line_end
        assert item.score > 0
        assert item.reason.strip()
        assert item.snippet
    scores = [item.score for item in items]
    assert scores == sorted(scores, reverse=True)


def test_chunks_are_not_repeated_and_k_is_respected(indexed_audit):
    with get_session_factory()() as session:
        four = retrieve_context(session, indexed_audit, AUTHZ_QUESTION, k=4)
        many = retrieve_context(session, indexed_audit, AUTHZ_QUESTION, k=30)

    assert len(four) == 4
    assert len({item.chunk_id for item in many}) == len(many)
    assert len(many) > 4


def test_a_query_with_no_words_needs_no_focus_to_return_nothing(indexed_audit):
    with get_session_factory()() as session:
        assert retrieve_context(session, indexed_audit, "   ") == []


def test_retrieval_is_scoped_to_one_audit(indexed_audit):
    with get_session_factory()() as session:
        assert retrieve_context(session, uuid.uuid4(), AUTHZ_QUESTION) == []
        assert naive_top_k(session, uuid.uuid4(), AUTHZ_QUESTION) == []


# --- focus symbol ----------------------------------------------------------


def test_focus_symbol_is_anchored_and_pulls_in_its_callers(indexed_audit):
    with get_session_factory()() as session:
        items = retrieve_context(session, indexed_audit, "", focus_symbol="get_order", k=10)

    assert (items[0].file_path, items[0].symbol_name) == GET_ORDER
    assert "focus symbol" in items[0].reason
    callers = [item for item in items if "caller of get_order" in item.reason]
    assert callers, [(item.symbol_name, item.reason) for item in items]
    assert READ_ORDER in located(callers)


# --- the baseline ----------------------------------------------------------


def test_naive_top_k_is_vector_only_and_returns_k(indexed_audit):
    with get_session_factory()() as session:
        hits = naive_top_k(session, indexed_audit, AUTHZ_QUESTION, k=5)

    assert len(hits) == 5
    assert {hit.matched_by for hit in hits} == {"vector"}
    assert [hit.score for hit in hits] == sorted((hit.score for hit in hits), reverse=True)


def test_naive_top_k_misses_the_middleware_that_hybrid_finds(indexed_audit):
    """The comparison the step asks for, asserted rather than eyeballed."""
    with get_session_factory()() as session:
        naive = naive_top_k(session, indexed_audit, AUTHZ_QUESTION, k=5)
        hybrid = retrieve_context(session, indexed_audit, AUTHZ_QUESTION, k=10)

    assert GET_CURRENT_USER not in located(naive), located(naive)
    assert GET_CURRENT_USER in located(hybrid)
