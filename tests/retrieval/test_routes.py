import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from openai import APIConnectionError
from psycopg_pool import AsyncConnectionPool
from testcontainers.community.postgres import PostgresContainer

import app.search as retrieval
from app.config import settings
from app.ingestion.pipeline import INDEX_PREFIX


def vector(x, y=0):
    return [float(x), float(y)] + [0.0] * (settings.embedding_dimensions - 2)


@pytest.fixture
async def client():
    app = FastAPI()
    app.include_router(retrieval.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
def embed(monkeypatch):
    create = AsyncMock(
        return_value=SimpleNamespace(
            data=[SimpleNamespace(index=0, embedding=vector(1))]
        )
    )
    client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
    monkeypatch.setattr(retrieval, "get_ai_client", lambda: client)
    return create


@pytest.mark.parametrize(
    "body",
    [
        {"query": " \t"},
        {"query": "text", "k": 0},
        {"query": "text", "k": 101},
        {"query": "text", "k": True},
        {"query": "text", "path_prefix": "/absolute"},
        {"query": "text", "exclude_paths": ["folder/../note.md"]},
        {"query": "text", "exclude_paths": ["bad\x00path"]},
    ],
)
async def test_search_validates_before_embedding(client, embed, body):
    response = await client.post("/search", json=body)
    assert response.status_code == 422
    embed.assert_not_called()


@pytest.mark.parametrize(
    "params", [{}, {"path": "../note.md"}, {"path": "note.md", "k": 0}]
)
async def test_similar_validates_path_and_limit(client, params):
    response = await client.get("/similar", params=params)
    assert response.status_code == 422


async def test_embedding_connection_failure_returns_bad_gateway(client, embed):
    embed.side_effect = APIConnectionError(
        request=httpx.Request("POST", "http://embedding/embeddings")
    )
    response = await client.post("/search", json={"query": "text"})
    assert response.status_code == 502
    assert "http://embedding" not in response.text


@pytest.mark.parametrize("embedding", [[], [0.0], None, "invalid"])
async def test_invalid_embedding_returns_bad_gateway(client, embed, embedding):
    embed.return_value.data[0].embedding = embedding
    response = await client.post("/search", json={"query": "text"})
    assert response.status_code == 502


async def test_missing_pool_returns_service_unavailable(client, embed):
    response = await client.get("/similar", params={"path": "note.md"})
    assert response.status_code == 503
    embed.assert_not_called()


@pytest.mark.parametrize("stage", ["embedding", "search database", "similar database"])
async def test_retrieval_deadline_cancels_pending_work(
    client, embed, monkeypatch, stage
):
    monkeypatch.setattr(settings, "retrieval_timeout_seconds", 0.05)
    cancelled = asyncio.Event()

    async def stall(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    if stage == "embedding":
        embed.side_effect = stall
    else:
        monkeypatch.setattr(retrieval, "fetch_rows", stall)

    async with asyncio.timeout(2):
        if stage == "similar database":
            response = await client.get("/similar", params={"path": "target.md"})
        else:
            response = await client.post("/search", json={"query": "text"})

    assert response.status_code == 504
    assert response.json() == {"detail": "Retrieval timed out"}
    assert cancelled.is_set()


@pytest.fixture(scope="module")
def postgres_url():
    schema = Path(__file__).parents[2] / "scripts" / "postgres-schema.sql"
    with PostgresContainer("pgvector/pgvector:pg17", driver=None) as postgres:
        code, output = postgres.exec(
            [
                "psql",
                "-U",
                postgres.username,
                "-d",
                postgres.dbname,
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                schema.read_text(),
            ]
        )
        assert code == 0, output.decode()
        yield postgres.get_connection_url()


@pytest.fixture
async def indexed_notes(postgres_url, monkeypatch):
    async with AsyncConnectionPool(postgres_url, min_size=1, open=False) as pool:
        await pool.open(wait=True)
        async with pool.connection() as conn:
            await conn.execute("TRUNCATE documents, ingested_files")
            async with conn.cursor() as cursor:
                await cursor.executemany(
                    """
                    INSERT INTO documents (source_file, chunk_index, heading, content, embedding)
                    VALUES (%s, %s, %s, %s, %s::vector)
                    """,
                    [
                        (r"Ops%_\Notes/B.md", 0, "B", "b", str(vector(1))),
                        (r"Ops%_\Notes/A.md", 2, None, "second chunk", str(vector(1))),
                        (
                            r"Ops%_\Notes/A.md",
                            1,
                            "Top > Section",
                            "first chunk",
                            str(vector(1)),
                        ),
                        ("OpsXA\\Notes/C.md", 0, "C", "c", str(vector(1))),
                        ("target.md", 0, None, "x", str(vector(1))),
                        ("target.md", 1, None, "y", str(vector(0, 1))),
                        ("diagonal.md", 0, None, "diag", str(vector(1, 1))),
                        ("zero.md", 0, None, "zero", str(vector(0))),
                        ("missing-vector.md", 0, None, "missing", None),
                        ("legacy.txt", 0, "Old heading", "legacy", str(vector(1))),
                        ("/legacy/absolute.md", 0, None, "legacy", str(vector(1))),
                        ("orphan.md", 0, None, "no index marker", str(vector(1))),
                    ],
                )
            await conn.execute(
                """
                INSERT INTO ingested_files (source_file, content_hash)
                SELECT DISTINCT source_file, %s || repeat('a', 64)
                FROM documents WHERE source_file <> ALL(%s)
                """,
                (INDEX_PREFIX, ["legacy.txt", "/legacy/absolute.md", "orphan.md"]),
            )
            await conn.execute(
                """
                INSERT INTO ingested_files (source_file, content_hash)
                VALUES ('legacy.txt', repeat('b', 64)),
                       ('/legacy/absolute.md', repeat('c', 64))
                """
            )
        monkeypatch.setattr(retrieval, "get_pg_pool", lambda: pool)
        yield pool


@pytest.mark.integration
async def test_search_returns_ordered_citations_with_literal_filters(
    client, embed, indexed_notes
):
    response = await client.post(
        "/search",
        json={"query": "a paraphrase", "path_prefix": "Ops%_\\Notes/", "k": 3},
    )
    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "source_file": r"Ops%_\Notes/A.md",
            "chunk_index": 1,
            "heading": "Top > Section",
            "content": "first chunk",
            "score": 1.0,
        },
        {
            "source_file": r"Ops%_\Notes/A.md",
            "chunk_index": 2,
            "heading": None,
            "content": "second chunk",
            "score": 1.0,
        },
        {
            "source_file": r"Ops%_\Notes/B.md",
            "chunk_index": 0,
            "heading": "B",
            "content": "b",
            "score": 1.0,
        },
    ]
    embed.assert_awaited_once_with(
        model=settings.embedding_model, input=["a paraphrase"]
    )

    response = await client.post(
        "/search",
        json={
            "query": "text",
            "path_prefix": "Ops",
            "exclude_paths": [r"Ops%_\Notes/A.md"],
            "k": 2,
        },
    )
    assert response.status_code == 200, response.text
    assert [row["source_file"] for row in response.json()] == [
        r"Ops%_\Notes/B.md",
        r"OpsXA\Notes/C.md",
    ]
    response = await client.post(
        "/search", json={"query": "text", "path_prefix": "' OR true--"}
    )
    assert response.status_code == 200 and response.json() == []


@pytest.mark.integration
async def test_similar_uses_centroids_and_returns_one_row_per_note(
    client, embed, indexed_notes
):
    response = await client.get("/similar", params={"path": "target.md", "k": 3})
    assert response.status_code == 200, response.text
    rows = response.json()
    assert [row["source_file"] for row in rows] == [
        "diagonal.md",
        r"Ops%_\Notes/A.md",
        r"Ops%_\Notes/B.md",
    ]
    assert [row["score"] for row in rows] == pytest.approx([1, 2**-0.5, 2**-0.5])

    for path in ("absent.md", "zero.md", "missing-vector.md"):
        response = await client.get("/similar", params={"path": path})
        assert response.status_code == 404

    response = await client.get(
        "/similar",
        params={
            "path": "target.md",
            "exclude_paths": [
                "diagonal.md",
                r"Ops%_\Notes/A.md",
                r"Ops%_\Notes/B.md",
                r"OpsXA\Notes/C.md",
            ],
        },
    )
    assert response.status_code == 200 and response.json() == []
    embed.assert_not_called()


@pytest.mark.integration
async def test_http_exposes_only_current_index_and_ranks_unequal_scores(
    client, embed, indexed_notes
):
    response = await client.post("/search", json={"query": "text"})
    assert response.status_code == 200, response.text
    results = response.json()
    assert [row["source_file"] for row in results] == [
        r"Ops%_\Notes/A.md",
        r"Ops%_\Notes/A.md",
        r"Ops%_\Notes/B.md",
        r"OpsXA\Notes/C.md",
        "target.md",
        "diagonal.md",
        "target.md",
    ]
    assert [row["score"] for row in results] == pytest.approx(
        [1, 1, 1, 1, 1, 2**-0.5, 0]
    )

    response = await client.get("/similar", params={"path": "target.md"})
    assert response.status_code == 200, response.text
    assert [row["source_file"] for row in response.json()] == [
        "diagonal.md",
        r"Ops%_\Notes/A.md",
        r"Ops%_\Notes/B.md",
        r"OpsXA\Notes/C.md",
    ]
    for path in ("legacy.txt", "orphan.md"):
        response = await client.get("/similar", params={"path": path})
        assert response.status_code == 404

    async with indexed_notes.connection() as conn:
        await conn.execute(
            "UPDATE ingested_files SET content_hash = %s || content_hash WHERE source_file = %s",
            (INDEX_PREFIX, "legacy.txt"),
        )
    response = await client.post(
        "/search", json={"query": "text", "path_prefix": "legacy.txt"}
    )
    assert response.status_code == 200, response.text
    assert [row["source_file"] for row in response.json()] == ["legacy.txt"]
    response = await client.get("/similar", params={"path": "legacy.txt", "k": 1})
    assert response.status_code == 200, response.text
    assert response.json() == [{"source_file": r"Ops%_\Notes/A.md", "score": 1.0}]


@pytest.mark.integration
async def test_database_deadline_recovers_after_lock(
    client, indexed_notes, monkeypatch
):
    monkeypatch.setattr(settings, "retrieval_timeout_seconds", 0.25)
    async with indexed_notes.connection() as lock:
        await lock.execute("LOCK TABLE documents IN ACCESS EXCLUSIVE MODE")
        async with asyncio.timeout(5):
            response = await client.get("/similar", params={"path": "target.md"})
        assert response.status_code == 504, response.text

    response = await client.get("/similar", params={"path": "target.md", "k": 1})
    assert response.status_code == 200, response.text
    assert response.json() == [{"source_file": "diagonal.md", "score": 1.0}]
