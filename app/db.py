import asyncio
import logging

from psycopg_pool import AsyncConnectionPool

from app.config import settings

logger = logging.getLogger(__name__)

_pg_pool: AsyncConnectionPool | None = None


async def init_postgres():
    connection_string = settings.postgres_url
    global _pg_pool
    _pg_pool = AsyncConnectionPool(
        connection_string,
        min_size=1,
        max_size=10,
        open=False,
        kwargs={"connect_timeout": max(2, int(settings.dependency_timeout_seconds))},
    )
    await _pg_pool.open()


async def check_postgres():
    async with asyncio.timeout(settings.dependency_timeout_seconds):
        async with get_pg_pool().connection() as conn:
            await conn.execute("SELECT heading, tags, embedding FROM documents LIMIT 0")
            await conn.execute("SELECT 1 FROM ingested_files LIMIT 0")


async def close_postgres():
    global _pg_pool
    pool, _pg_pool = _pg_pool, None
    if pool is not None:
        await pool.close()
        logger.info("Postgres connection pool closed")


def get_pg_pool() -> AsyncConnectionPool:
    if _pg_pool is None:
        raise RuntimeError("Postgres pool not initialised")
    return _pg_pool
