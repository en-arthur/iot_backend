"""MQTT listener and publisher.

One background task owns the broker connection:

* subscribes to ``boards/#`` and processes messages sequentially
* reconnects with exponential backoff (with jitter) when the broker drops
* publishes relay commands through a queue so HTTP handlers never touch the
  network directly

All database writes happen here or in the reading batcher. HTTP handlers only
read (ownership/online checks) and enqueue a command.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import aiomqtt
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.batching import ReadingBatcher, ReadingRow
from app.config import Settings
from app.models import Board, RelayState
from app.topics import (
    parse_relay_payload,
    parse_sensor_payload,
    parse_status_payload,
    parse_topic,
    relay_set_topic,
    subscription_topic,
)

logger = logging.getLogger(__name__)


class MqttUnavailable(RuntimeError):
    """Raised when a command cannot be published right now."""


@dataclass(slots=True)
class _PublishRequest:
    topic: str
    payload: str
    qos: int
    future: asyncio.Future[None] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


@dataclass
class MqttStatus:
    connected: bool = False
    last_error: str | None = None
    last_message_at: datetime | None = None
    last_connected_at: datetime | None = None
    reconnect_count: int = 0
    messages_processed: int = 0


class MqttService:
    """Manage the broker connection for the lifetime of the app."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        batcher: ReadingBatcher,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._batcher = batcher
        self._prefix = settings.mqtt_topic_prefix.strip("/") or "boards"
        self._qos = settings.mqtt_qos
        self._client_id = settings.mqtt_client_id or f"relayboard-backend-{uuid4().hex[:8]}"

        self._status = MqttStatus()
        self._client: aiomqtt.Client | None = None
        self._publish_queue: asyncio.Queue[_PublishRequest] = asyncio.Queue(maxsize=1_000)
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    # --- lifecycle --------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._status.connected

    def status(self) -> dict[str, Any]:
        return {
            "connected": self._status.connected,
            "client_id": self._client_id,
            "topic_prefix": self._prefix,
            "last_error": self._status.last_error,
            "last_message_at": _iso(self._status.last_message_at),
            "last_connected_at": _iso(self._status.last_connected_at),
            "reconnect_count": self._status.reconnect_count,
            "messages_processed": self._status.messages_processed,
            "pending_publishes": self._publish_queue.qsize(),
        }

    async def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="mqtt-listener")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._fail_pending_publishes("MQTT client is shutting down")
        self._status.connected = False

    # --- publishing -------------------------------------------------------

    async def publish_relay_command(self, board_id: str, relay_number: int, state: str) -> None:
        """Publish a relay command with QoS 1, waiting for the broker to accept it."""
        if not self._status.connected or self._client is None:
            raise MqttUnavailable("MQTT broker is not connected")

        loop = asyncio.get_running_loop()
        request = _PublishRequest(
            topic=relay_set_topic(board_id, relay_number, prefix=self._prefix),
            payload=state,
            qos=1,
            future=loop.create_future(),
        )

        try:
            self._publish_queue.put_nowait(request)
        except asyncio.QueueFull as exc:
            raise MqttUnavailable("MQTT publish queue is full") from exc

        try:
            await asyncio.wait_for(request.future, timeout=self._settings.mqtt_publish_timeout)
        except TimeoutError as exc:
            raise MqttUnavailable("Timed out publishing to the MQTT broker") from exc

    async def _publisher(self, client: aiomqtt.Client) -> None:
        while True:
            request = await self._publish_queue.get()
            try:
                await client.publish(request.topic, request.payload, qos=request.qos)
            except asyncio.CancelledError:
                if not request.future.done():
                    request.future.cancel()
                raise
            except Exception as exc:  # noqa: BLE001 - report to the caller
                logger.warning(
                    "MQTT publish failed",
                    extra={"topic": request.topic, "error": str(exc)},
                )
                if not request.future.done():
                    request.future.set_exception(MqttUnavailable("Could not publish to MQTT"))
            else:
                logger.debug("MQTT publish accepted", extra={"topic": request.topic})
                if not request.future.done():
                    request.future.set_result(None)
            finally:
                self._publish_queue.task_done()

    def _fail_pending_publishes(self, message: str) -> None:
        while True:
            try:
                request = self._publish_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if not request.future.done():
                request.future.set_exception(MqttUnavailable(message))
            self._publish_queue.task_done()

    # --- connection loop --------------------------------------------------

    async def _run(self) -> None:
        delay = max(0.1, self._settings.mqtt_reconnect_min_delay)

        while not self._stopping:
            try:
                await self._listen()
                if self._stopping:
                    break
                # The broker closed the connection cleanly; pause before
                # reconnecting so a flapping broker cannot cause a hot loop.
                logger.warning("MQTT connection closed unexpectedly; reconnecting")
                await asyncio.sleep(max(0.1, self._settings.mqtt_reconnect_min_delay))
                delay = max(0.1, self._settings.mqtt_reconnect_min_delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on anything
                self._status.connected = False
                self._status.last_error = str(exc)
                self._status.reconnect_count += 1
                logger.warning(
                    "MQTT connection lost; retrying",
                    extra={
                        "error": str(exc),
                        "retry_in_seconds": round(delay, 2),
                        "reconnect_count": self._status.reconnect_count,
                    },
                )
                await asyncio.sleep(delay + random.uniform(0, delay * 0.1))
                delay = min(delay * 2, self._settings.mqtt_reconnect_max_delay)

    async def _listen(self) -> None:
        logger.info(
            "connecting to MQTT broker",
            extra={
                "host": self._settings.mqtt_host,
                "port": self._settings.mqtt_port,
                "client_id": self._client_id,
            },
        )

        async with aiomqtt.Client(
            hostname=self._settings.mqtt_host,
            port=self._settings.mqtt_port,
            username=self._settings.mqtt_username,
            password=self._settings.mqtt_password,
            identifier=self._client_id,
            keepalive=self._settings.mqtt_keepalive,
        ) as client:
            self._client = client
            self._status.connected = True
            self._status.last_error = None
            self._status.last_connected_at = datetime.now(UTC)

            await client.subscribe(subscription_topic(prefix=self._prefix), qos=1)
            logger.info(
                "subscribed to MQTT topics",
                extra={"topic": subscription_topic(prefix=self._prefix)},
            )

            publisher = asyncio.create_task(self._publisher(client), name="mqtt-publisher")
            try:
                async for message in client.messages:
                    await self._handle_message(message)
            finally:
                publisher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await publisher
                self._client = None
                self._status.connected = False
                self._fail_pending_publishes("MQTT connection closed")
                logger.info("MQTT connection closed")

    # --- message handling -------------------------------------------------

    async def _handle_message(self, message: aiomqtt.Message) -> None:
        topic = message.topic.value
        parsed = parse_topic(topic, prefix=self._prefix)

        if parsed is None:
            logger.warning("ignoring malformed MQTT topic", extra={"topic": topic})
            return

        self._status.last_message_at = datetime.now(UTC)
        self._status.messages_processed += 1

        try:
            if parsed.kind == "status":
                await self._handle_status(parsed.board_id, message.payload)
            elif parsed.kind == "sensor":
                await self._handle_sensor(parsed.board_id, parsed.sensor or "", message.payload)
            elif parsed.kind == "relay_state":
                await self._handle_relay_state(
                    parsed.board_id,
                    parsed.relay_number or 0,
                    message.payload,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad message must not kill us
            logger.exception(
                "failed to process MQTT message",
                extra={"topic": topic, "error": str(exc)},
            )

    async def _handle_status(self, board_id: str, payload: Any) -> None:
        status = parse_status_payload(payload)
        if status is None:
            logger.warning(
                "ignoring malformed status payload",
                extra={"board_id": board_id, "payload": _preview(payload)},
            )
            return

        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            await _ensure_board(session, board_id)
            await session.execute(
                update(Board).where(Board.id == board_id).values(status=status, last_seen=now)
            )

        logger.info("board status updated", extra={"board_id": board_id, "status": status})

    async def _handle_sensor(self, board_id: str, sensor: str, payload: Any) -> None:
        reading = parse_sensor_payload(payload)
        if reading is None:
            logger.warning(
                "ignoring malformed sensor payload",
                extra={
                    "board_id": board_id,
                    "sensor": sensor,
                    "payload": _preview(payload),
                },
            )
            return

        await self._batcher.add(
            ReadingRow(
                board_id=board_id,
                sensor=sensor,
                value=reading.value,
                ts=reading.ts,
            )
        )

    async def _handle_relay_state(self, board_id: str, relay_number: int, payload: Any) -> None:
        state = parse_relay_payload(payload)
        if state is None:
            logger.warning(
                "ignoring malformed relay payload",
                extra={
                    "board_id": board_id,
                    "relay": relay_number,
                    "payload": _preview(payload),
                },
            )
            return

        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            await _ensure_board(session, board_id)
            statement = pg_insert(RelayState).values(
                board_id=board_id,
                relay_number=relay_number,
                state=state,
                updated_at=now,
            )
            statement = statement.on_conflict_do_update(
                index_elements=["board_id", "relay_number"],
                set_={
                    "state": statement.excluded.state,
                    "updated_at": statement.excluded.updated_at,
                },
            )
            await session.execute(statement)

        logger.info(
            "relay state updated",
            extra={"board_id": board_id, "relay": relay_number, "state": state},
        )


async def _ensure_board(session: AsyncSession, board_id: str) -> None:
    """Create the board row if the firmware reported before it was registered."""
    await session.execute(
        pg_insert(Board)
        .values(id=board_id, name=board_id)
        .on_conflict_do_nothing(index_elements=["id"])
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _preview(payload: Any, limit: int = 120) -> str:
    if isinstance(payload, (bytes, bytearray)):
        text = bytes(payload)[:limit].decode("utf-8", errors="replace")
    else:
        text = str(payload)[:limit]
    return text
