"""Batching of sensor readings.

A board publishing at 20 Hz should not produce 20 INSERT round-trips per second.
Readings are queued and flushed either when the batch is full or after a short
interval (default 200 ms), which keeps latency low while collapsing bursts.

``readings`` gets one row per message; ``latest_readings`` is upserted in the
same transaction and only moves forward in time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Board, LatestReading, Reading

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReadingRow:
    board_id: str
    sensor: str
    value: float
    ts: datetime


class ReadingBatcher:
    """Queue sensor readings and write them in batches."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_batch_size: int = 100,
        flush_interval: float = 0.2,
        queue_max_size: int = 10_000,
    ) -> None:
        self._session_factory = session_factory
        self._max_batch_size = max(1, max_batch_size)
        self._flush_interval = max(0.01, flush_interval)
        self._queue: asyncio.Queue[ReadingRow] = asyncio.Queue(maxsize=queue_max_size)
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

        self._written = 0
        self._dropped = 0
        self._last_error: str | None = None

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def stats(self) -> dict[str, int | str | None]:
        return {
            "pending": self.pending,
            "written": self._written,
            "dropped": self._dropped,
            "last_error": self._last_error,
        }

    async def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="reading-batcher")

    async def stop(self) -> None:
        """Cancel the flush loop and write whatever is still queued."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

        remaining = self._drain_queue()
        if remaining:
            await self._write_batch(remaining)

    async def add(self, reading: ReadingRow) -> None:
        """Queue a reading, dropping it (with a warning) if the queue is full."""
        try:
            self._queue.put_nowait(reading)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 100 == 1:
                logger.warning(
                    "reading queue is full; dropping readings",
                    extra={
                        "board_id": reading.board_id,
                        "sensor": reading.sensor,
                        "dropped_total": self._dropped,
                    },
                )

    async def _run(self) -> None:
        while True:
            batch = [await self._queue.get()]
            deadline = asyncio.get_running_loop().time() + self._flush_interval

            while len(batch) < self._max_batch_size:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self._queue.get(), remaining))
                except TimeoutError:
                    break

            try:
                await self._write_batch(batch)
            except Exception as exc:  # noqa: BLE001 - keep the listener alive
                self._last_error = str(exc)
                logger.exception(
                    "failed to write reading batch",
                    extra={"count": len(batch), "error": str(exc)},
                )

    def _drain_queue(self) -> list[ReadingRow]:
        rows: list[ReadingRow] = []
        while True:
            try:
                rows.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                return rows

    async def _write_batch(self, rows: Iterable[ReadingRow]) -> None:
        batch = list(rows)
        if not batch:
            return

        # Auto-create unknown boards so a sensor message that arrives before the
        # status message still satisfies the readings foreign key.
        board_ids = sorted({row.board_id for row in batch})
        latest = self._latest_per_sensor(batch)

        async with self._session_factory() as session, session.begin():
            await session.execute(
                pg_insert(Board)
                .values([{"id": board_id, "name": board_id} for board_id in board_ids])
                .on_conflict_do_nothing(index_elements=["id"])
            )

            await session.execute(
                pg_insert(Reading),
                [
                    {
                        "board_id": row.board_id,
                        "sensor": row.sensor,
                        "value": row.value,
                        "ts": row.ts,
                    }
                    for row in batch
                ],
            )

            latest_stmt = pg_insert(LatestReading).values(list(latest.values()))
            latest_stmt = latest_stmt.on_conflict_do_update(
                index_elements=["board_id", "sensor"],
                set_={
                    "value": latest_stmt.excluded.value,
                    "ts": latest_stmt.excluded.ts,
                },
                # Out-of-order messages must not roll the latest value back.
                where=LatestReading.ts <= latest_stmt.excluded.ts,
            )
            await session.execute(latest_stmt)

        self._written += len(batch)
        logger.debug(
            "wrote reading batch",
            extra={
                "readings": len(batch),
                "boards": len(board_ids),
                "latest_rows": len(latest),
            },
        )

    @staticmethod
    def _latest_per_sensor(batch: Iterable[ReadingRow]) -> dict[tuple[str, str], dict[str, object]]:
        latest: dict[tuple[str, str], ReadingRow] = {}
        for row in batch:
            key = (row.board_id, row.sensor)
            current = latest.get(key)
            if current is None or row.ts >= current.ts:
                latest[key] = row

        return {
            key: {
                "board_id": row.board_id,
                "sensor": row.sensor,
                "value": row.value,
                "ts": row.ts,
            }
            for key, row in latest.items()
        }
