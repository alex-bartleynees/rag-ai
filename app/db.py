import logging

from psycopg_pool import AsyncConnectionPool

from app.config import settings

logger = logging.getLogger(__name__)

_pg_pool: AsyncConnectionPool | None = None


async def init_postgres():
    connection_string = settings.postgres_url
    global _pg_pool
    _pg_pool = AsyncConnectionPool(
        connection_string, min_size=1, max_size=10, open=False
    )
    await _pg_pool.open()
    await _pg_pool.wait()
    logger.info("Postgres connection pool initialized")


async def close_postgres():
    global _pg_pool
    if _pg_pool:
        await _pg_pool.close()
        _pg_pool = None
        logger.info("Postgres connection pool closed")


def get_pg_pool() -> AsyncConnectionPool:
    if _pg_pool is None:
        raise RuntimeError("Postgres pool not initialised")
    return _pg_pool
