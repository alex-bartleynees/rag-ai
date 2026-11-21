import asyncio
import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from openai import APIConnectionError, APIStatusError
from psycopg import Error
from psycopg_pool import PoolTimeout

from app.ai_client import check_embedding, close_ai_client, init_ai_client
from app.config import settings
from app.db import check_postgres, close_postgres, init_postgres
from app.watcher import start_file_watcher, stop_file_watcher

logger = logging.getLogger(__name__)


async def wait_for_dependency(name, check, retryable):
    delay = settings.startup_retry_seconds
    while True:
        try:
            return await check()
        except retryable as error:
            logger.warning(
                "Waiting for %s (%s), retrying in %.1fs",
                name,
                type(error).__name__,
                delay,
            )
        await asyncio.sleep(delay)
        delay = min(delay * 2, settings.startup_retry_max_seconds)


def check_docs_directory():
    # Inotify allocates descriptors before registering the directory watch.
    # Check that the directory can be opened before constructing an observer.
    with os.scandir(settings.docs_dir):
        pass


async def start_watcher():
    await asyncio.to_thread(check_docs_directory)
    return start_file_watcher()


async def start_dependencies(app: FastAPI):
    await wait_for_dependency(
        "Postgres schema", check_postgres, (Error, PoolTimeout, TimeoutError)
    )
    await wait_for_dependency(
        "Ollama embeddings",
        check_embedding,
        (APIConnectionError, APIStatusError, TimeoutError, ValueError),
    )
    app.state.file_watcher = await wait_for_dependency(
        "DOCS_DIR watcher", start_watcher, OSError
    )


def log_startup_failure(task):
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.error("Startup failed", exc_info=error)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.file_watcher = None
    async with AsyncExitStack() as resources:
        resources.push_async_callback(close_postgres)
        await init_postgres()
        resources.push_async_callback(close_ai_client)
        await init_ai_client()
        startup = asyncio.create_task(start_dependencies(app), name="rag_ai_startup")
        startup.add_done_callback(log_startup_failure)
        app.state.startup_task = startup
        try:
            yield
        finally:
            if not startup.done():
                startup.cancel()
            try:
                await asyncio.gather(startup, return_exceptions=True)
            finally:
                watcher = app.state.file_watcher
                app.state.file_watcher = None
                if watcher is not None:
                    await stop_file_watcher(watcher)
