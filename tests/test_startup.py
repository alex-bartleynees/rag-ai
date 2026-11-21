import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.main import app
from app.watcher import FileWatcher, start_file_watcher, stop_file_watcher

lifecycle = importlib.import_module("app.lifespan")
main = importlib.import_module("app.main")


@pytest.fixture
def resources(monkeypatch):
    calls = []
    for name in (
        "init_postgres",
        "init_ai_client",
        "close_postgres",
        "close_ai_client",
    ):
        monkeypatch.setattr(
            lifecycle, name, AsyncMock(side_effect=lambda name=name: calls.append(name))
        )
    monkeypatch.setattr(
        lifecycle,
        "stop_file_watcher",
        AsyncMock(side_effect=lambda _: calls.append("stop_watcher")),
    )
    watcher = SimpleNamespace(status={"state": "idle"}, pending={})
    monkeypatch.setattr(lifecycle, "start_file_watcher", Mock(return_value=watcher))
    monkeypatch.setattr(lifecycle.settings, "startup_retry_seconds", 0.001)
    return calls


async def test_readiness_waits_for_dependencies_and_mounts_retrieval(
    monkeypatch, resources
):
    postgres = AsyncMock(side_effect=[TimeoutError, None])
    release_embedding = asyncio.Event()

    async def retry_then_wait():
        if embedding.await_count == 1:
            raise TimeoutError
        await release_embedding.wait()

    embedding = AsyncMock(side_effect=retry_then_wait)
    model_health = AsyncMock()
    monkeypatch.setattr(lifecycle, "check_postgres", postgres)
    monkeypatch.setattr(lifecycle, "check_embedding", embedding)
    monkeypatch.setattr(main, "check_postgres", AsyncMock())
    monkeypatch.setattr(main, "check_embedding_model", model_health)

    async with (
        lifecycle.lifespan(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        assert (await client.post("/search", json={})).status_code == 503
        assert (await client.get("/health")).status_code == 503
        release_embedding.set()
        await asyncio.wait_for(asyncio.shield(app.state.startup_task), timeout=2)
        assert (await client.get("/health")).json() == {
            "ready": True,
            "startup": "ready",
            "postgres": "ok",
            "ollama": "ok",
        }
        assert (await client.get("/ingestion/status")).json() == {
            "state": "idle",
            "queued": 0,
        }
        assert (await client.post("/search", json={})).status_code == 422
        assert (await client.get("/similar")).status_code == 422
        model_health.side_effect = TimeoutError
        assert (await client.get("/health")).status_code == 503
        model_health.side_effect = None
        assert (await client.get("/health")).status_code == 200

    assert postgres.await_count == embedding.await_count == 2
    lifecycle.start_file_watcher.assert_called_once()
    assert resources == [
        "init_postgres",
        "init_ai_client",
        "stop_watcher",
        "close_ai_client",
        "close_postgres",
    ]


async def test_shutdown_cancels_a_pending_probe(monkeypatch, resources):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_probe():
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(lifecycle, "check_postgres", blocked_probe)
    async with asyncio.timeout(2):
        async with lifecycle.lifespan(app):
            await entered.wait()

    assert cancelled.is_set()
    lifecycle.start_file_watcher.assert_not_called()
    assert resources == [
        "init_postgres",
        "init_ai_client",
        "close_ai_client",
        "close_postgres",
    ]


async def test_readiness_recovers_when_docs_directory_appears(
    monkeypatch, resources, tmp_path, caplog
):
    docs = tmp_path / "docs"
    monkeypatch.setattr(lifecycle.settings, "docs_dir", str(docs))
    monkeypatch.setattr(FileWatcher, "reconcile", AsyncMock())
    postgres = AsyncMock()
    embedding = AsyncMock()
    monkeypatch.setattr(lifecycle, "check_postgres", postgres)
    monkeypatch.setattr(lifecycle, "check_embedding", embedding)
    monkeypatch.setattr(main, "check_postgres", AsyncMock())
    monkeypatch.setattr(main, "check_embedding_model", AsyncMock())

    check_directory = lifecycle.check_docs_directory
    loop = asyncio.get_running_loop()
    failed = asyncio.Event()

    def check():
        try:
            check_directory()
        except FileNotFoundError:
            loop.call_soon_threadsafe(failed.set)
            raise

    started = []

    def start():
        watcher = start_file_watcher()
        started.append(watcher)
        return watcher

    start_attempts = Mock(side_effect=[FileNotFoundError("Directory disappeared"), None])

    def start_after_preflight():
        start_attempts()
        return start()

    stop = AsyncMock(wraps=stop_file_watcher)
    monkeypatch.setattr(lifecycle, "check_docs_directory", check)
    monkeypatch.setattr(lifecycle, "start_file_watcher", start_after_preflight)
    monkeypatch.setattr(lifecycle, "stop_file_watcher", stop)

    async with (
        asyncio.timeout(2),
        lifecycle.lifespan(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        await failed.wait()
        unavailable = await client.get("/health")
        assert unavailable.status_code == 503
        assert unavailable.json()["startup"] == "starting"
        assert app.state.file_watcher is None
        start_attempts.assert_not_called()

        docs.mkdir()
        await asyncio.shield(app.state.startup_task)
        ready = await client.get("/health")
        assert ready.status_code == 200
        assert ready.json()["startup"] == "ready"
        assert started == [app.state.file_watcher]

    assert "Waiting for DOCS_DIR watcher (FileNotFoundError)" in caplog.text
    assert start_attempts.call_count == 2
    postgres.assert_awaited_once()
    embedding.assert_awaited_once()
    stop.assert_awaited_once_with(started[0])
    assert not started[0].observer.is_alive()
    assert started[0].task.cancelled()


async def test_shutdown_cancels_watcher_startup_backoff(monkeypatch, resources):
    attempted = asyncio.Event()

    def missing_docs():
        attempted.set()
        raise FileNotFoundError("Missing document directory")

    start = Mock(side_effect=missing_docs)
    monkeypatch.setattr(lifecycle, "start_file_watcher", start)
    monkeypatch.setattr(lifecycle, "check_docs_directory", Mock())
    monkeypatch.setattr(lifecycle, "check_postgres", AsyncMock())
    monkeypatch.setattr(lifecycle, "check_embedding", AsyncMock())
    monkeypatch.setattr(lifecycle.settings, "startup_retry_seconds", 30)

    async with asyncio.timeout(2):
        async with lifecycle.lifespan(app):
            await attempted.wait()

    assert app.state.startup_task.cancelled()
    assert app.state.file_watcher is None
    start.assert_called_once()
    lifecycle.stop_file_watcher.assert_not_awaited()
    assert resources == [
        "init_postgres",
        "init_ai_client",
        "close_ai_client",
        "close_postgres",
    ]
