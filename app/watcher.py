import asyncio
import logging
import os
import stat
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from app.ai_client import get_ai_client
from app.config import settings
from app.db import get_pg_pool
from app.ingestion.chunker import is_ignored, iter_files
from app.ingestion.pipeline import (
    INDEX_PREFIX,
    ingest_file,
    legacy_relative_sources,
    remove_ingested_documents,
)

logger = logging.getLogger(__name__)


class FileWatcher(FileSystemEventHandler):
    """Watch first, reconcile on startup, and process one file at a time."""

    def __init__(self, directory: str):
        self.configured_directory = Path(directory)
        self.directory = Path(os.path.abspath(directory))
        self.loop = asyncio.get_running_loop()
        self.observer = Observer()
        self.pending: dict[Path, tuple[float, int]] = {}
        self.wake = asyncio.Event()
        self.rescan = True
        self.task = None
        self.status = {"state": "starting", "current_file": None, "ingested": 0,
                       "unchanged": 0, "deleted": 0, "failed": 0, "discovered": 0}

    def start(self):
        self.observer.schedule(self, str(self.directory), recursive=True)
        self.observer.start()
        self.task = asyncio.create_task(self.run(), name="file_watcher")
        self.task.add_done_callback(self.worker_finished)
        logger.info("Started file watcher on directory: %s", self.directory)

    def worker_finished(self, task):
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            self.status["state"] = "failed"
            logger.exception("Ingestion worker stopped unexpectedly")

    async def stop(self):
        self.observer.stop()
        await asyncio.to_thread(self.observer.join)
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.status["state"] = "stopped"
        logger.info("Stopped file watcher")

    def on_any_event(self, event):
        if event.event_type not in {"created", "modified", "deleted", "moved"}:
            return
        # Parent-directory modifications accompany ordinary writes; ignore those.
        if event.is_directory and event.event_type == "modified":
            return
        paths = [event.src_path]
        if event.event_type == "moved":
            paths.append(event.dest_path)
        for path in paths:
            self.loop.call_soon_threadsafe(self.enqueue, Path(os.fsdecode(path)), event.is_directory)

    def enqueue(self, path: Path, is_directory: bool = False):
        path = Path(os.path.abspath(path))
        if is_ignored(path, self.directory):
            return
        if is_directory:
            self.rescan = True
        elif path.suffix[1:] in settings.supported_file_types:
            self.pending[path] = (self.loop.time() + 1.0, 0)
        else:
            return
        self.wake.set()

    async def reconcile(self):
        self.status["state"] = "scanning"
        # Complete discovery before cleanup; traversal errors abort deletion.
        files = await asyncio.to_thread(lambda: list(iter_files(self.directory)))
        self.status["discovered"] = len(files)
        pool = get_pg_pool()
        async with pool.connection() as conn:
            cursor = await conn.execute(
                """
                SELECT source_file, content_hash FROM ingested_files
                UNION ALL
                SELECT DISTINCT d.source_file, NULL FROM documents d
                WHERE NOT EXISTS (SELECT 1 FROM ingested_files i WHERE i.source_file = d.source_file)
                """
            )
            indexed = await cursor.fetchall()
        for source, digest in indexed:
            path = Path(source)
            candidate = self.directory / path
            # Older bulk ingestion could store a path relative to the process.
            if (
                not path.is_absolute()
                and not self.configured_directory.is_absolute()
                and digest is not None
                and not digest.startswith(INDEX_PREFIX)
            ):
                legacy = Path(os.path.abspath(path))
                if legacy.is_relative_to(self.directory):
                    candidate = legacy
            absolute = Path(os.path.abspath(candidate))
            if not absolute.is_relative_to(self.directory):
                continue
            try:
                file_stat = await asyncio.to_thread(absolute.stat)
                missing = not stat.S_ISREG(file_stat.st_mode)
            except (FileNotFoundError, NotADirectoryError):
                missing = True
            if missing or is_ignored(absolute, self.directory) or path.suffix[1:] not in settings.supported_file_types:
                await remove_ingested_documents(pool, path)
                self.status["deleted"] += 1
        for path in files:
            self.pending.setdefault(path, (self.loop.time(), 0))
        logger.info("Reconciled document directory: discovered=%d queued=%d", len(files), len(self.pending))

    async def run(self):
        while True:
            self.wake.clear()
            if self.rescan:
                self.rescan = False
                try:
                    await self.reconcile()
                except Exception:
                    self.status["failed"] += 1
                    logger.exception("Document directory reconciliation failed")
            if not self.pending:
                self.status["state"] = "idle"
                logger.info("Ingestion queue drained: %s", self.status)
                await self.wake.wait()
                continue
            path = min(self.pending, key=lambda item: self.pending[item][0])
            deadline, attempt = self.pending[path]
            delay = deadline - self.loop.time()
            if delay > 0:
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=delay)
                except TimeoutError:
                    pass
                continue
            del self.pending[path]
            self.status.update(state="ingesting", current_file=str(path))
            try:
                try:
                    file_stat = await asyncio.to_thread(path.stat)
                    missing = not stat.S_ISREG(file_stat.st_mode)
                except (FileNotFoundError, NotADirectoryError):
                    missing = True
                if missing:
                    source = path.relative_to(self.directory)
                    await remove_ingested_documents(
                        get_pg_pool(), source, path,
                        legacy_sources=legacy_relative_sources(self.configured_directory, source),
                    )
                    self.status["deleted"] += 1
                else:
                    changed = await ingest_file(
                        path, get_ai_client(), get_pg_pool(), self.configured_directory
                    )
                    self.status["ingested" if changed else "unchanged"] += 1
            except Exception:
                logger.exception("File ingestion failed: %s attempt=%d", path, attempt + 1)
                if attempt < 2:
                    self.pending.setdefault(path, (self.loop.time() + 2 ** (attempt + 1), attempt + 1))
                else:
                    self.status["failed"] += 1
            finally:
                self.status["current_file"] = None


def start_file_watcher():
    watcher = FileWatcher(settings.docs_dir)
    watcher.start()
    return watcher


async def stop_file_watcher(watcher: FileWatcher):
    await watcher.stop()
