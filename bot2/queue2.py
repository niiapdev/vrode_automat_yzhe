"""Очередь задач для bot2 (клон queue_manager.py с расширенными полями).

Поля задачи: ссылка | серии | озвучка | плеер | качество.
Обработка строго последовательная (одна видеокарта).
"""

import asyncio
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from paths2 import JOBS_DB

logger = logging.getLogger("queue2")

MAX_ERROR_LEN = 800


@dataclass
class Job2:
    id: int
    url: str
    episodes: str
    dub: str
    player: str
    quality: int
    user_id: int
    status: str
    result: Optional[str] = None
    extra: list = field(default_factory=list)  # доп. сообщения (таймкоды)
    error: Optional[str] = None
    created_at: Optional[str] = None


class QueueManager2:
    def __init__(self, processor: Callable, db_path: Path = JOBS_DB):
        """
        processor: async (job: Job2, progress: async (msg) -> None) -> str
        """
        self._processor = processor
        self._db_path = Path(db_path)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._lock = threading.Lock()
        self._worker_task: Optional[asyncio.Task] = None
        self._running: Optional[Job2] = None
        self._notify_done: Optional[Callable[[Job2], Awaitable[None]]] = None
        self._notify_error: Optional[Callable[[Job2, str], Awaitable[None]]] = None
        self._progress: Optional[Callable[[Job2, str], Awaitable[None]]] = None
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT NOT NULL,
                        episodes TEXT NOT NULL DEFAULT '1',
                        dub TEXT NOT NULL DEFAULT '-',
                        player TEXT NOT NULL DEFAULT '-',
                        quality INTEGER NOT NULL DEFAULT 1080,
                        user_id INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        result TEXT,
                        error TEXT,
                        created_at TEXT
                    )
                    """
                )
                # Задачи, прерванные перезапуском бота, не должны висеть вечно.
                self._conn.execute(
                    "UPDATE jobs SET status = ?, error = ? WHERE status = ?",
                    ("failed", "Задача прервана перезапуском бота", "processing"),
                )

    def set_notifiers(self, on_done, on_error, on_progress) -> None:
        self._notify_done = on_done
        self._notify_error = on_error
        self._progress = on_progress

    @property
    def running_job(self) -> Optional[Job2]:
        return self._running

    @staticmethod
    def _ts() -> str:
        return datetime.now().isoformat(sep=" ", timespec="seconds")

    def submit(
        self,
        url: str,
        user_id: int,
        episodes: str = "1",
        dub: str = "-",
        player: str = "-",
        quality: int = 1080,
    ) -> Job2:
        created_at = self._ts()
        with self._lock:
            with self._conn:
                cur = self._conn.execute(
                    "INSERT INTO jobs (url, episodes, dub, player, quality, user_id, status, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (url, episodes, dub, player, int(quality), user_id, "queued", created_at),
                )
                job_id = cur.lastrowid
        job = Job2(
            id=job_id,
            url=url,
            episodes=episodes,
            dub=dub,
            player=player,
            quality=int(quality),
            user_id=user_id,
            status="queued",
            created_at=created_at,
        )
        self._wake_worker()
        return job

    def cancel_user_jobs(self, user_id: int) -> int:
        with self._lock:
            with self._conn:
                cur = self._conn.execute(
                    "UPDATE jobs SET status = ?, error = ? WHERE user_id = ? AND status = ?",
                    ("cancelled", "Отменено пользователем", user_id, "queued"),
                )
                return cur.rowcount

    def list_user_jobs(self, user_id: int, limit: int = 5) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, url, episodes, dub, player, quality, status, error"
                " FROM jobs WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return rows

    def _wake_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker_loop())

    def _fetch_queued(self) -> Optional[Job2]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, url, episodes, dub, player, quality, user_id, status,"
                " result, error, created_at FROM jobs WHERE status = ? ORDER BY id ASC LIMIT 1",
                ("queued",),
            ).fetchone()
        if row is None:
            return None
        return Job2(
            id=row[0],
            url=row[1],
            episodes=row[2],
            dub=row[3],
            player=row[4],
            quality=row[5],
            user_id=row[6],
            status=row[7],
            result=row[8],
            error=row[9],
            created_at=row[10],
        )

    def _mark_status(
        self,
        job_id: int,
        status: str,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE jobs SET status = ?, result = ?, error = ? WHERE id = ?",
                    (status, result, error, job_id),
                )

    async def _worker_loop(self) -> None:
        try:
            while True:
                job = self._fetch_queued()
                if job is None:
                    break
                self._running = job
                self._mark_status(job.id, "processing")

                async def progress(message: str, _job: Job2 = job) -> None:
                    if self._progress is None:
                        return
                    try:
                        await self._progress(_job, message)
                    except Exception:
                        logger.exception("Не удалось отправить прогресс")

                try:
                    outcome = await self._processor(job, progress)
                    # processor может вернуть строку (старый контракт) или
                    # (итог, [доп. сообщения]) — например, таймкоды.
                    if isinstance(outcome, tuple):
                        result, extras = outcome[0], outcome[1] or []
                    else:
                        result, extras = outcome, []
                    extras = [str(m) for m in extras if str(m).strip()]
                    self._mark_status(job.id, "done", result=str(result), error=None)
                    job.result = str(result)
                    job.extra = extras
                    job.status = "done"
                    if self._notify_done is not None:
                        try:
                            await self._notify_done(job)
                        except Exception:
                            logger.exception("Ошибка уведомления о готовности")
                except Exception as exc:
                    msg = (str(exc)[:MAX_ERROR_LEN]) or "Неизвестная ошибка"
                    self._mark_status(job.id, "failed", result=None, error=msg)
                    logger.exception("Задача %s упала", job.id)
                    job.status = "failed"
                    job.error = msg
                    # Ошибку отправляет только _notify_error — иначе в ВК придёт два сообщения.
                    if self._notify_error is not None:
                        try:
                            await self._notify_error(job, msg)
                        except Exception:
                            logger.exception("Ошибка отправки сообщения об ошибке")
                finally:
                    self._running = None
        finally:
            self._worker_task = None
