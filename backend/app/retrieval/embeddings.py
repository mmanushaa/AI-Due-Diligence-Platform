"""Embedding providers and chunk embedding (step 2.3).

Two providers share one interface:

* ``HashingEmbeddingProvider`` is offline and deterministic. It hashes
  identifiers and words into a fixed-width vector, so cosine similarity behaves
  like a weighted token overlap. It exists so the pipeline and the tests run
  without a model, a key or a network call — not because it understands text.
* ``GatewayEmbeddingProvider`` calls the LLM gateway's ``/embeddings`` endpoint
  (LiteLLM style, OpenAI-compatible) with the model name from settings.

The dimension comes from settings and must equal ``models.EMBEDDING_DIM``: the
column is ``vector(1536)``, so a different value is refused at construction with
a clear message instead of failing later inside Postgres.

No key is ever logged or included in an exception message.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Protocol, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, settings as default_settings
from app.db.models import EMBEDDING_DIM, Chunk

TOKEN_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Function words carry no signal in a bag-of-tokens vector and only dilute it.
STOPWORDS = frozenset(
    """
    a an and are as at be been but by can could did do does for from had has have how
    i if in into is it its me my no not of on or our our out over should so some than
    that the their them then there these they this to was we were what when where which
    who why will with would you your
    """.split()
)
# A word inside an identifier (``calculate`` in ``calculate_total``) counts for
# less than a word used on its own.
SUBTOKEN_WEIGHT = 0.35


class EmbeddingError(RuntimeError):
    """Raised when embeddings could not be produced. Never contains a key."""


class EmbeddingProvider(Protocol):
    """The thin interface an embedding backend has to satisfy."""

    model: str
    dimensions: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per input text, in the same order."""


def _validate_dimensions(dimensions: int) -> int:
    if dimensions != EMBEDDING_DIM:
        raise EmbeddingError(
            f"embedding dimensions {dimensions} do not match the vector({EMBEDDING_DIM}) column; "
            "change EMBEDDING_DIMENSIONS and migrate together"
        )
    return dimensions


# --- offline provider ------------------------------------------------------


def _tokens(text: str) -> list[tuple[str, float]]:
    """Whole identifiers plus their snake/camel parts, with weights."""
    weighted: list[tuple[str, float]] = []
    for token in TOKEN_PATTERN.findall(text):
        lowered = token.lower()
        if lowered not in STOPWORDS:
            weighted.append((lowered, 1.0))
        parts = [part for part in lowered.split("_") if part]
        parts.extend(
            part.lower() for part in CAMEL_BOUNDARY.split(token) if part and part != token
        )
        for part in parts:
            if part != lowered and part not in STOPWORDS:
                weighted.append((part, SUBTOKEN_WEIGHT))
    return weighted


class HashingEmbeddingProvider:
    """Deterministic bag-of-tokens vectors; offline and dependency free."""

    def __init__(self, model: str = "hashing-v1", dimensions: int = EMBEDDING_DIM) -> None:
        self.model = model
        self.dimensions = _validate_dimensions(dimensions)

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token, weight in _tokens(text):
            digest = hashlib.blake2b(token.encode("utf8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            index = value % self.dimensions
            sign = 1.0 if (value >> 63) & 1 else -1.0
            vector[index] += sign * weight
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return vector

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]


# --- gateway provider ------------------------------------------------------


class GatewayEmbeddingProvider:
    """Calls an OpenAI-compatible ``/embeddings`` endpoint through the gateway."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int = EMBEDDING_DIM,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not base_url:
            raise EmbeddingError("LITELLM_BASE_URL is required for the gateway embedding provider")
        if not api_key:
            raise EmbeddingError("LITELLM_API_KEY is required for the gateway embedding provider")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key  # never logged, never returned
        self.model = model
        self.dimensions = _validate_dimensions(dimensions)
        self.timeout_seconds = timeout_seconds

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = json.dumps(
            {"model": self.model, "input": list(texts), "dimensions": self.dimensions}
        ).encode("utf8")
        request = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            # The status and a short body are safe; the request headers are not echoed.
            detail = exc.read().decode("utf8", "replace")[:200]
            raise EmbeddingError(f"embedding request failed: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise EmbeddingError(f"embedding request failed: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise EmbeddingError("embedding response was not JSON") from exc

        data = body.get("data") or []
        if len(data) != len(texts):
            raise EmbeddingError(
                f"embedding response had {len(data)} vectors for {len(texts)} inputs"
            )
        vectors: list[list[float]] = []
        for item in data:
            vector = item.get("embedding")
            if not isinstance(vector, list) or len(vector) != self.dimensions:
                raise EmbeddingError(
                    "embedding response vector did not match the configured dimensions"
                )
            vectors.append([float(value) for value in vector])
        return vectors


def get_provider(settings: Settings | None = None) -> EmbeddingProvider:
    """The configured provider: the gateway when it is set up, else offline hashing."""
    settings = settings or default_settings
    if settings.embedding_provider.lower() == "gateway":
        api_key = settings.litellm_api_key.get_secret_value() if settings.litellm_api_key else ""
        return GatewayEmbeddingProvider(
            base_url=settings.litellm_base_url or "",
            api_key=api_key,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            timeout_seconds=settings.embedding_timeout_seconds,
        )
    return HashingEmbeddingProvider(
        model=settings.embedding_model if settings.embedding_provider == "hashing" else "hashing-v1",
        dimensions=settings.embedding_dimensions,
    )


# --- storing vectors -------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingSummary:
    chunks: int
    batches: int
    model: str
    dimensions: int
    skipped: int = 0


def embed_chunks(
    session: Session,
    audit_id: uuid.UUID,
    *,
    provider: EmbeddingProvider | None = None,
    batch_size: int | None = None,
    force: bool = False,
    settings: Settings | None = None,
) -> EmbeddingSummary:
    """Embed an audit's chunks in batches and store the vectors.

    Only chunks without a vector are embedded unless ``force`` is set. One batch
    is one provider call, so a 500-chunk audit is a few calls rather than 500.
    """
    settings = settings or default_settings
    provider = provider or get_provider(settings)
    batch = max(1, batch_size or settings.embedding_batch_size)

    statement = select(Chunk).where(Chunk.audit_id == audit_id).order_by(Chunk.created_at, Chunk.id)
    if not force:
        statement = statement.where(Chunk.embedding.is_(None))
    pending = list(session.execute(statement).scalars())
    skipped = 0
    if not pending and not force:
        total = session.execute(
            select(func.count()).select_from(Chunk).where(Chunk.audit_id == audit_id)
        ).scalar_one()
        return EmbeddingSummary(
            chunks=0, batches=0, model=provider.model, dimensions=provider.dimensions, skipped=total
        )

    batches = 0
    embedded = 0
    for start in range(0, len(pending), batch):
        slice_ = pending[start : start + batch]
        vectors = provider.embed([chunk.content for chunk in slice_])
        if len(vectors) != len(slice_):
            raise EmbeddingError(
                f"provider returned {len(vectors)} vectors for {len(slice_)} chunks"
            )
        for chunk, vector in zip(slice_, vectors):
            chunk.embedding = vector
        batches += 1
        embedded += len(slice_)
        session.flush()

    session.commit()
    return EmbeddingSummary(
        chunks=embedded,
        batches=batches,
        model=provider.model,
        dimensions=provider.dimensions,
        skipped=skipped,
    )
