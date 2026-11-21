import asyncio
import hashlib
import logging
import os
from collections.abc import Iterable
from pathlib import Path

from openai import AsyncOpenAI
from psycopg.connection_async import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from app.config import settings
from app.ingestion.chunker import (
    chunk_text,
    extract_text_from_file,
    iter_files,
    markdown_tags,
    split_frontmatter,
)
from app.ingestion.embedder import embed_chunks
from app.ingestion.models import EmbeddedChunk

logger = logging.getLogger(__name__)

# Distinguish citation-ready records from the legacy unversioned text digest.
INDEX_PREFIX = "2:"


async def ingest_documents(
    docs_dir: str, client: AsyncOpenAI, pool: AsyncConnectionPool
):
    """Ingest documents from the specified directory, chunk them, and embed the chunks."""
    logger.info("Starting ingestion of documents from %s", docs_dir)
    root = Path(docs_dir)
    for file_path in iter_files(Path(os.path.abspath(root))):
        await ingest_file(file_path, client, pool, root)
    logger.info("Completed ingestion of documents from %s", docs_dir)


async def ingest_file(
    file_path: Path, client: AsyncOpenAI, pool: AsyncConnectionPool, docs_dir: Path | None = None
) -> bool:
    """Prepare embeddings before atomically replacing a file's indexed version."""
    root = Path(settings.docs_dir) if docs_dir is None else docs_dir
    absolute = Path(os.path.abspath(file_path))
    source = absolute.relative_to(Path(os.path.abspath(root)))
    logger.debug("Processing file: %s", file_path)
    text = await asyncio.to_thread(extract_text_from_file, file_path)
    if text is None:
        raise ValueError(f"Could not extract text from {file_path}")
    content_hash = f"{INDEX_PREFIX}{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
    if await check_if_ingested(pool, source, content_hash):
        return False
    tags = markdown_tags(split_frontmatter(text)[0]) if source.suffix == ".md" else []
    embedded = [chunk async for chunk in embed_chunks(chunk_text(source, text), client)]
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("DELETE FROM documents WHERE source_file = %s", (source.as_posix(),))
        await delete_source(conn, absolute)
        for alias in legacy_relative_sources(root, source):
            await delete_source(conn, alias, legacy_only=True)
        for chunk in embedded:
            await save_embedded_chunk(conn, chunk, tags)
        await mark_as_ingested(conn, source, content_hash, len(embedded))
    logger.info("Ingested file: %s chunks=%d", file_path, len(embedded))
    return True


async def save_embedded_chunk(
    conn: AsyncConnection, embedded_chunk: EmbeddedChunk, tags: list[str]
):
    logger.debug("Saving embedded chunk: %s - %s", embedded_chunk.source, embedded_chunk.position)
    await conn.execute(
        """
            INSERT INTO documents (source_file, chunk_index, heading, content, embedding, tags)
            VALUES (%s, %s, %s, %s, %s::vector, %s)
        """,
        (
            embedded_chunk.source.as_posix(),
            embedded_chunk.position,
            embedded_chunk.heading,
            embedded_chunk.text,
            str(embedded_chunk.embedding),
            tags,
        ),
    )


async def check_if_ingested(
    pool: AsyncConnectionPool, file_path: Path, content_hash: str
) -> bool:
    async with pool.connection() as conn:
        cursor = conn.cursor()
        await cursor.execute(
            "SELECT 1 from ingested_files WHERE source_file = %s AND content_hash = %s",
            (file_path.as_posix(), content_hash),
        )
        row = await cursor.fetchone()
        return row is not None


async def mark_as_ingested(
    conn: AsyncConnection, file_path: Path, content_hash: str, chunk_count: int = 0
):
    cursor = conn.cursor()
    await cursor.execute(
        """
            INSERT INTO ingested_files (source_file, content_hash, chunk_count) 
            VALUES (%s, %s, %s)
            ON CONFLICT (source_file) DO UPDATE 
            SET content_hash = EXCLUDED.content_hash, 
                chunk_count = EXCLUDED.chunk_count, 
                updated_at = NOW()
            """,
        (file_path.as_posix(), content_hash, chunk_count),
    )


def legacy_relative_sources(root: Path, source: Path) -> Iterable[Path]:
    if not root.is_absolute() and root / source != source:
        yield root / source


async def delete_source(conn: AsyncConnection, source: Path, *, legacy_only: bool = False):
    key = (source.as_posix(),)
    if legacy_only:
        cursor = await conn.execute(
            "SELECT 1 FROM ingested_files WHERE source_file = %s AND content_hash NOT LIKE %s",
            (*key, INDEX_PREFIX + "%"),
        )
        if await cursor.fetchone() is None:
            return
    await conn.execute("DELETE FROM documents WHERE source_file = %s", key)
    await conn.execute("DELETE FROM ingested_files WHERE source_file = %s", key)


async def remove_ingested_documents(
    pool: AsyncConnectionPool, *sources: Path, legacy_sources: Iterable[Path] = ()
):
    """Atomically remove indexed chunks and file bookkeeping for each key."""
    async with pool.connection() as conn, conn.transaction():
        for source in sources:
            await delete_source(conn, source)
        for source in legacy_sources:
            await delete_source(conn, source, legacy_only=True)
