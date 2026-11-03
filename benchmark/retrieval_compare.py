#!/usr/bin/env python3
"""Compare hybrid retrieval with plain vector search on the planted authz gap.

Usage (from the repository root, with the backend virtualenv):

    backend/.venv/Scripts/python benchmark/retrieval_compare.py
    backend/.venv/Scripts/python benchmark/retrieval_compare.py --repo js_app

The script indexes one fixture into a throwaway audit, runs
``retrieve_context`` (lexical + semantic + symbol, fused with reciprocal rank
fusion, then widened along the call graph and metadata) and ``naive_top_k``
(vector only) for the same question, and prints both rankings. It then reports
which of the four pieces of the planted cross-file issue each method surfaced:

    route      the handler declared on the unprotected route
    auth       the authentication helper that the route ignores
    database   the data-access function the handler reaches
    config     the settings/manifest the code reads

The fixture repositories are test data: they are read as text and never
executed, imported or handed to a shell. Requires Postgres (docker compose up
postgres) with the migrations applied; nothing else.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.db.enums import AuditStatus, SourceType  # noqa: E402
from app.db.models import Audit, Repository  # noqa: E402
from app.db.session import get_session_factory  # noqa: E402
from app.indexing.service import index_audit, index_graph  # noqa: E402
from app.intake.filtering import index_snapshot  # noqa: E402
from app.retrieval.embeddings import HashingEmbeddingProvider, embed_chunks  # noqa: E402
from app.retrieval.search import ContextItem, SearchHit, naive_top_k, retrieve_context  # noqa: E402

# One question per fixture, phrased the way an auditor would ask it rather than
# in the vocabulary of the file that has to be found.
QUERIES = {
    "python_app": (
        "an order endpoint that returns somebody's order by id without checking "
        "who is logged in"
    ),
    "js_app": (
        "a users endpoint that returns somebody's record by id without checking "
        "who is logged in"
    ),
}


@dataclass(frozen=True)
class Expectation:
    label: str
    file_path: str
    symbol: str


# The planted cross-file issues from benchmark/expected.json:
#
# py-authz-01: app/routes/orders.py read_order has no auth dependency, while
# app/routes/search.py routes use app/auth.py get_current_user, and the handler
# reads through app/database.py get_order.
#
# js-authz-01: the admin route reaches src/db/index.js findUserById without
# going through src/auth/requireAuth.js, which the users routes use.
#
# ``symbol`` is the name the parser stored; ``None`` means any chunk of that
# file counts, which is how a config file (no symbol) is checked.
EXPECTATIONS = {
    "python_app": (
        Expectation("route", "app/routes/orders.py", "read_order"),
        Expectation("auth", "app/auth.py", "get_current_user"),
        Expectation("database", "app/database.py", "get_order"),
        Expectation("config", "app/config.py", "Settings"),
    ),
    "js_app": (
        Expectation("route", "src/routes/admin.js", "GET /users/:id"),
        Expectation("auth", "src/auth/requireAuth.js", "requireAuth"),
        Expectation("database", "src/db/index.js", "findUserById"),
        Expectation("config", "src/config.js", None),
    ),
}


def build_audit(session, repo_name: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Index, graph and embed one fixture into a fresh audit."""
    root = (HERE / "repos" / repo_name).resolve()
    if not root.is_dir():
        raise SystemExit(f"fixture not found: {root}")

    repository = Repository(
        name=f"retrieval-compare-{repo_name}-{uuid.uuid4().hex[:6]}",
        source_type=SourceType.ZIP,
        storage_path=str(root),
    )
    session.add(repository)
    session.commit()
    session.refresh(repository)

    audit = Audit(repository_id=repository.id, status=AuditStatus.QUEUED)
    session.add(audit)
    session.commit()
    session.refresh(audit)

    index_snapshot(session, audit.id, root)
    indexing = index_audit(session, audit.id, snapshot_root=root)
    graph = index_graph(session, audit.id, snapshot_root=root)
    embeddings = embed_chunks(
        session, audit.id, provider=HashingEmbeddingProvider(), batch_size=32
    )
    print(
        f"indexed {repo_name}: {indexing.files_indexed} files, {indexing.symbols} symbols, "
        f"{indexing.chunks} chunks, {graph.module_edges} module edges, "
        f"{graph.call_edges} call edges, {embeddings.chunks} vectors"
    )
    return repository.id, audit.id


def cleanup(session, repository_id: uuid.UUID, audit_id: uuid.UUID) -> None:
    repository = session.get(Repository, repository_id)
    if repository is not None:
        repository.storage_path = None  # keep the fixture untouched on delete
    session.delete(session.get(Audit, audit_id))
    session.commit()
    session.delete(repository)
    session.commit()


def show_context(items: list[ContextItem]) -> None:
    for position, item in enumerate(items, 1):
        name = item.symbol_name or Path(item.file_path).name
        print(
            f"  {position:>2}. {name:<18} {item.file_path}:{item.line_start}-{item.line_end}"
            f"  score={item.score:.4f}  [{item.reason}]"
        )


def show_hits(hits: list[SearchHit]) -> None:
    for position, hit in enumerate(hits, 1):
        name = hit.symbol_name or Path(hit.file_path).name
        print(
            f"  {position:>2}. {name:<18} {hit.file_path}:{hit.line_start}-{hit.line_end}"
            f"  similarity={hit.score:.3f}"
        )


def matched(items: list, expectations: tuple[Expectation, ...]) -> dict[str, bool]:
    """Whether each piece is present, matched on file and symbol.

    A named piece needs the same file and the same symbol name; matching only
    the file would credit an unrelated import that happens to sit beside the
    function that matters. A piece without a symbol (a config file) matches any
    chunk of that file.
    """
    seen = {
        (getattr(item, "file_path", ""), getattr(item, "symbol_name", None)) for item in items
    }
    result: dict[str, bool] = {}
    for expectation in expectations:
        if expectation.symbol is None:
            result[expectation.label] = any(
                path == expectation.file_path for path, _ in seen
            )
        else:
            result[expectation.label] = (expectation.file_path, expectation.symbol) in seen
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="python_app", choices=sorted(EXPECTATIONS))
    parser.add_argument("--query", default=None, help="override the fixture's question")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--naive-k", type=int, default=5)
    args = parser.parse_args()

    query = args.query or QUERIES[args.repo]

    session = get_session_factory()()
    repository_id = audit_id = None
    try:
        repository_id, audit_id = build_audit(session, args.repo)

        print(f"\nquestion: {query!r}\n")

        hybrid = retrieve_context(session, audit_id, query, k=args.k)
        print(f"retrieve_context (k={args.k}) - fused, graph and metadata expanded:")
        show_context(hybrid)

        naive = naive_top_k(session, audit_id, query, k=args.naive_k)
        print(f"\nnaive_top_k (k={args.naive_k}) - vector search only:")
        show_hits(naive)

        expectations = EXPECTATIONS[args.repo]
        hybrid_found = matched(hybrid, expectations)
        naive_found = matched(naive, expectations)

        print("\nplanted unauthorized route - items found:")
        print(f"  {'piece':<10} {'retrieve_context':<18} {'naive_top_k'}")
        for expectation in expectations:
            label = expectation.label
            print(
                f"  {label:<10} {'yes' if hybrid_found[label] else 'no':<18} "
                f"{'yes' if naive_found[label] else 'no'}"
            )

        missed = [label for label, found in naive_found.items() if not found]
        print(
            "\nnaive search missed: "
            + (", ".join(missed) if missed else "nothing on this query")
        )
        return 0
    finally:
        if audit_id is not None and repository_id is not None:
            cleanup(session, repository_id, audit_id)
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
