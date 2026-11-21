import asyncio
import logging
import math
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from openai import APIError, APITimeoutError
from psycopg import OperationalError
from pydantic import AfterValidator, BaseModel, Field

from app.ai_client import get_ai_client
from app.config import settings
from app.db import get_pg_pool
from app.ingestion.pipeline import INDEX_PREFIX

logger = logging.getLogger(__name__)
router = APIRouter(tags=["retrieval"])

DEFAULT_K = 10
MAX_K = 100
MAX_EXCLUSIONS = 100

SCORE_DESCRIPTION = "Cosine similarity from -1 to 1. Higher scores are closer."


@asynccontextmanager
async def retrieval_deadline():
    try:
        async with asyncio.timeout(settings.retrieval_timeout_seconds):
            yield
    except TimeoutError as exc:
        raise HTTPException(504, "Retrieval timed out") from exc


def validate_relative_path(value: str, *, allow_trailing_slash: bool = False) -> str:
    path = value.removesuffix("/") if allow_trailing_slash else value
    if (
        not path.strip()
        or "\x00" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise ValueError("Expected a vault-relative POSIX path")
    return value


def validate_prefix(value: str) -> str:
    return validate_relative_path(value, allow_trailing_slash=True)


def validate_query(value: str) -> str:
    if not value.strip():
        raise ValueError("Query must contain non-whitespace text")
    return value


NotePath = Annotated[
    str, Field(max_length=4096), AfterValidator(validate_relative_path)
]
PathPrefix = Annotated[str, Field(max_length=4096), AfterValidator(validate_prefix)]
SearchText = Annotated[str, Field(max_length=8192), AfterValidator(validate_query)]


class SearchRequest(BaseModel):
    query: SearchText
    k: int = Field(default=DEFAULT_K, ge=1, le=MAX_K, strict=True)
    path_prefix: PathPrefix | None = None
    exclude_paths: list[NotePath] = Field(
        default_factory=list, max_length=MAX_EXCLUSIONS
    )


class SimilarNote(BaseModel):
    source_file: str
    score: float = Field(description=SCORE_DESCRIPTION)


class SearchResult(SimilarNote):
    chunk_index: int
    heading: str | None
    content: str


SEARCH_SQL = """
    SELECT d.source_file, chunk_index, heading, content,
           1 - (embedding <=> %(embedding)s::vector) AS score
    FROM documents AS d
    JOIN ingested_files AS i ON i.source_file = d.source_file
    WHERE embedding IS NOT NULL AND vector_norm(embedding) > 0
      AND starts_with(i.content_hash, %(index_prefix)s)
      AND (%(prefix)s::text IS NULL OR starts_with(d.source_file, %(prefix)s::text))
      AND d.source_file <> ALL(%(excluded)s::text[])
    ORDER BY score DESC, d.source_file COLLATE "C", chunk_index
    LIMIT %(limit)s
"""

SIMILAR_SQL = """
    WITH indexed AS NOT MATERIALIZED (
        SELECT d.source_file, d.embedding
        FROM documents AS d
        JOIN ingested_files AS i ON i.source_file = d.source_file
        WHERE starts_with(i.content_hash, %(index_prefix)s)
          AND vector_norm(d.embedding) > 0
    ),
    target AS (
        SELECT avg(embedding) AS centroid
        FROM indexed
        WHERE source_file = %(path)s
    )
    SELECT neighbor.source_file, neighbor.score
    FROM target
    LEFT JOIN LATERAL (
        SELECT source_file, 1 - (avg(embedding) <=> target.centroid) AS score
        FROM indexed
        WHERE source_file <> %(path)s
          AND source_file <> ALL(%(excluded)s::text[])
        GROUP BY source_file
        HAVING vector_norm(avg(embedding)) > 0
        ORDER BY score DESC, source_file COLLATE "C"
        LIMIT %(limit)s
    ) AS neighbor ON true
    WHERE vector_norm(target.centroid) > 0
    ORDER BY neighbor.score DESC, neighbor.source_file COLLATE "C"
"""


async def embed_query(query: str) -> str:
    try:
        client = get_ai_client()
    except RuntimeError as exc:
        raise HTTPException(503, "Embedding service unavailable") from exc

    try:
        response = await client.embeddings.create(
            model=settings.embedding_model, input=[query]
        )
        if len(response.data) != 1 or response.data[0].index != 0:
            raise ValueError("Expected one embedding for input index 0")
        vector = response.data[0].embedding
        if (
            len(vector) != settings.embedding_dimensions
            or not all(
                math.isfinite(value) and abs(value) <= 3.4028234663852886e38
                for value in vector
            )
            or not any(abs(value) > 2**-150 for value in vector)
        ):
            raise ValueError(
                "Expected a finite, nonzero embedding of the configured dimension"
            )
    except APITimeoutError as exc:
        raise HTTPException(504, "Embedding service timed out") from exc
    except (APIError, ValueError, TypeError, AttributeError) as exc:
        logger.warning("Search embedding failed (%s)", type(exc).__name__)
        raise HTTPException(502, "Embedding service failed to embed the query") from exc

    # The shared pool uses no pgvector adapter. Bind the vector text as a parameter.
    return str(vector)


async def fetch_rows(sql: str, params: dict):
    try:
        pool = get_pg_pool()
    except RuntimeError as exc:
        raise HTTPException(503, "Retrieval database unavailable") from exc

    try:
        async with pool.connection() as conn:
            cursor = await conn.execute(sql, params)
            return await cursor.fetchall()
    except OperationalError as exc:
        logger.warning("Retrieval database unavailable (%s)", type(exc).__name__)
        raise HTTPException(503, "Retrieval database unavailable") from exc


@router.post("/search")
async def search(request: SearchRequest) -> list[SearchResult]:
    """Return matching chunks ranked by cosine similarity."""
    async with retrieval_deadline():
        rows = await fetch_rows(
            SEARCH_SQL,
            {
                "embedding": await embed_query(request.query),
                "prefix": request.path_prefix,
                "excluded": request.exclude_paths,
                "limit": request.k,
                "index_prefix": INDEX_PREFIX,
            },
        )
    return [
        SearchResult(
            source_file=source_file,
            chunk_index=chunk_index,
            heading=heading,
            content=content,
            score=score,
        )
        for source_file, chunk_index, heading, content, score in rows
    ]


@router.get("/similar")
async def similar(
    path: Annotated[NotePath, Query()],
    k: Annotated[int, Query(ge=1, le=MAX_K)] = DEFAULT_K,
    exclude_paths: Annotated[
        list[NotePath] | None, Query(max_length=MAX_EXCLUSIONS)
    ] = None,
) -> list[SimilarNote]:
    """Rank other notes by cosine similarity between mean chunk embeddings."""
    async with retrieval_deadline():
        rows = await fetch_rows(
            SIMILAR_SQL,
            {
                "path": path,
                "excluded": exclude_paths or [],
                "limit": k,
                "index_prefix": INDEX_PREFIX,
            },
        )
    if not rows:
        raise HTTPException(404, "No usable embedding found for the requested note")
    return [
        SimilarNote(source_file=source_file, score=score)
        for source_file, score in rows
        if source_file is not None
    ]
