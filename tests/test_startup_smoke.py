"""Opt-in live Ollama smoke with an isolated, disposable Postgres container."""

import asyncio
import os
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.main import app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RAG_AI_LIVE_SMOKE") != "1",
        reason="set RAG_AI_LIVE_SMOKE=1 to exercise local Ollama and disposable Postgres",
    ),
]


async def test_live_startup_and_ingestion(monkeypatch, tmp_path):
    from testcontainers.community.postgres import PostgresContainer

    ollama_url = "http://127.0.0.1:11434"
    model = "nomic-embed-text"
    async with httpx.AsyncClient(timeout=2, trust_env=False) as probe:
        try:
            response = await probe.get(f"{ollama_url}/api/tags")
            response.raise_for_status()
        except httpx.HTTPError as error:
            pytest.skip(f"Local Ollama unavailable: {error}")
        if not any(
            item["name"] in {model, f"{model}:latest"}
            for item in response.json()["models"]
        ):
            pytest.skip(f"Local Ollama does not have {model}")

    schema = Path(__file__).resolve().parents[1] / "scripts" / "postgres-schema.sql"
    (tmp_path / "smoke.md").write_text(
        "# Startup smoke\n\nRag-ai can index synthetic Markdown.\n"
    )
    with PostgresContainer("pgvector/pgvector:pg17", driver=None) as postgres:
        code, output = postgres.exec(
            [
                "psql",
                "-X",
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
        monkeypatch.setattr(settings, "postgres_url", postgres.get_connection_url())
        monkeypatch.setattr(settings, "docs_dir", str(tmp_path))
        monkeypatch.setattr(settings, "llm_base_url", f"{ollama_url}/v1")
        monkeypatch.setattr(settings, "embedding_model", model)
        monkeypatch.setattr(settings, "embedding_dimensions", 768)

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            async with asyncio.timeout(120):
                while (await client.get("/health")).status_code != 200:
                    await asyncio.sleep(0.25)
                while True:
                    status = (await client.get("/ingestion/status")).json()
                    assert status["failed"] == 0, status
                    if status["ingested"] == 1:
                        break
                    await asyncio.sleep(0.25)
                result = await client.post(
                    "/search", json={"query": "index a Markdown document"}
                )
                assert result.status_code == 200, result.text
                assert result.json()[0]["source_file"] == "smoke.md"
                assert result.json()[0]["heading"] == "Startup smoke"
