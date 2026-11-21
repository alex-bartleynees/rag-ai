import asyncio
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from openai import APIError
from psycopg import Error
from psycopg_pool import PoolTimeout

from app.ai_client import check_embedding_model
from app.db import check_postgres
from app.lifespan import lifespan
from app.logging_config import configure_logging
from app.middleware import RequestIDMiddleware
from app.search import router as search_router

configure_logging()
logger = logging.getLogger(__name__)

app = FastAPI(lifespan=lifespan)
app.include_router(search_router)


def startup_status(application: FastAPI) -> str:
    task = application.state.startup_task
    if not task.done():
        return "starting"
    if task.cancelled() or task.exception() is not None:
        return "failed"
    watcher = application.state.file_watcher
    if watcher is None or watcher.status["state"] in {"failed", "stopped"}:
        return "failed"
    return "ready"


@app.middleware("http")
async def require_startup(request: Request, call_next):
    if (
        request.url.path not in {"/health", "/ingestion/status"}
        and startup_status(request.app) != "ready"
    ):
        return JSONResponse(
            {"detail": "Service is starting or unavailable"}, status_code=503
        )
    return await call_next(request)


app.add_middleware(RequestIDMiddleware)


@app.get("/ingestion/status")
async def ingestion_status(request: Request):
    state = startup_status(request.app)
    if state != "ready":
        return JSONResponse({"state": state, "queued": 0}, status_code=503)
    watcher = request.app.state.file_watcher
    return {**watcher.status, "queued": len(watcher.pending)}


async def dependency_status(name, check):
    try:
        await check()
        return "ok"
    except (APIError, Error, PoolTimeout, TimeoutError, ValueError) as error:
        logger.warning("%s health check failed (%s)", name, type(error).__name__)
        return "error"


@app.get("/health")
async def health(request: Request):
    postgres, ollama = await asyncio.gather(
        dependency_status("Postgres", check_postgres),
        dependency_status("Ollama", check_embedding_model),
    )
    state = startup_status(request.app)
    ready = state == "ready" and postgres == ollama == "ok"
    return JSONResponse(
        {"ready": ready, "startup": state, "postgres": postgres, "ollama": ollama},
        status_code=200 if ready else 503,
    )
