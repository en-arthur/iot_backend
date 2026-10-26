"""MQTT topic and payload parsing.

The firmware contract is:

    boards/{board_id}/status             -> "online" | "offline"
    boards/{board_id}/sensors/{name}     -> {"value": 24.5, "ts": 1700000000}
    boards/{board_id}/relays/{n}/state   -> "on" | "off"

Every parser returns ``None`` for anything it does not understand. The MQTT
listener logs a warning and drops those messages; a malformed payload must never
take the listener down.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

TopicKind = Literal["status", "sensor", "relay_state"]

DEFAULT_TOPIC_PREFIX = "boards"

# Board IDs are printed on the hardware and typed by users when claiming, so
# keep them URL/path friendly and case-sensitive.
_BOARD_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SENSOR_NAME_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

# Relays are 1-based. The upper bound is a sanity limit, not a hardware limit.
MIN_RELAY_NUMBER = 1
MAX_RELAY_NUMBER = 999

# Sensor timestamps outside this window are treated as clock errors and
# replaced with the receive time.
_MIN_TIMESTAMP = datetime(2000, 1, 1, tzinfo=UTC)
_MAX_FUTURE_SKEW = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class ParsedTopic:
    """A recognised topic, split into its parts."""

    kind: TopicKind
    board_id: str
    sensor: str | None = None
    relay_number: int | None = None


@dataclass(frozen=True, slots=True)
class SensorReading:
    """A validated sensor payload."""

    value: float
    ts: datetime


Payload = str | bytes | bytearray


def decode_payload(payload: Payload) -> str | None:
    """Decode a raw MQTT payload as UTF-8, returning ``None`` if it is not text."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        try:
            return bytes(payload).decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None


def parse_topic(topic: str, *, prefix: str = DEFAULT_TOPIC_PREFIX) -> ParsedTopic | None:
    """Parse a topic into a :class:`ParsedTopic`, or ``None`` if unrecognised."""
    if not isinstance(topic, str):
        return None

    prefix = prefix.strip("/")
    if not prefix:
        return None

    topic = topic.strip()
    marker = f"{prefix}/"
    if not topic.startswith(marker):
        return None

    parts = topic[len(marker) :].split("/")
    if len(parts) < 2:
        return None

    board_id = parts[0]
    if not _BOARD_ID_RE.fullmatch(board_id):
        return None

    # boards/{id}/status
    if len(parts) == 2 and parts[1] == "status":
        return ParsedTopic(kind="status", board_id=board_id)

    # boards/{id}/sensors/{name}
    if len(parts) == 3 and parts[1] == "sensors":
        sensor = parts[2]
        if not _SENSOR_NAME_RE.fullmatch(sensor):
            return None
        return ParsedTopic(kind="sensor", board_id=board_id, sensor=sensor)

    # boards/{id}/relays/{n}/state
    if len(parts) == 4 and parts[1] == "relays" and parts[3] == "state":
        if not parts[2].isdigit():
            return None
        relay_number = int(parts[2])
        if not MIN_RELAY_NUMBER <= relay_number <= MAX_RELAY_NUMBER:
            return None
        return ParsedTopic(kind="relay_state", board_id=board_id, relay_number=relay_number)

    return None


def parse_status_payload(payload: Payload) -> str | None:
    """Return ``"online"``/``"offline"``, or ``None`` for anything else."""
    text = decode_payload(payload)
    if text is None:
        return None
    value = text.strip().lower()
    if value in {"online", "offline"}:
        return value
    return None


def parse_relay_payload(payload: Payload) -> str | None:
    """Return ``"on"``/``"off"``, or ``None`` for anything else."""
    text = decode_payload(payload)
    if text is None:
        return None
    value = text.strip().lower()
    if value in {"on", "off"}:
        return value
    return None


def parse_sensor_payload(payload: Payload, *, now: datetime | None = None) -> SensorReading | None:
    """Parse ``{"value": <number>, "ts": <unix seconds|ISO 8601>}``.

    ``ts`` is optional: when it is missing or implausible the receive time is
    used, because a board with a wrong clock should not poison the history.
    """
    text = decode_payload(payload)
    if text is None:
        return None

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    raw_value = data.get("value")
    # bool is a subclass of int, and NaN/Infinity are not useful readings.
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        return None

    value = float(raw_value)
    if not math.isfinite(value):
        return None

    received_at = now or datetime.now(UTC)
    ts = _parse_timestamp(data.get("ts"), received_at) or received_at
    return SensorReading(value=value, ts=ts)


def _parse_timestamp(raw: object, now: datetime) -> datetime | None:
    """Normalise a unix timestamp (seconds or milliseconds) or ISO 8601 string."""
    parsed: datetime | None = None

    if isinstance(raw, bool):
        return None

    if isinstance(raw, (int, float)):
        parsed = _from_unix(float(raw))
    elif isinstance(raw, str):
        candidate = raw.strip()
        if not candidate:
            return None
        try:
            parsed = _from_unix(float(candidate))
        except ValueError:
            parsed = _from_iso(candidate)

    if parsed is None:
        return None
    if parsed < _MIN_TIMESTAMP or parsed > now + _MAX_FUTURE_SKEW:
        return None
    return parsed


def _from_unix(value: float) -> datetime | None:
    if not math.isfinite(value) or value <= 0:
        return None
    # Accept milliseconds as a convenience; anything above 1e11 is not seconds
    # for the lifetime of this system.
    if value > 1e11:
        value /= 1000.0
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _from_iso(value: str) -> datetime | None:
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def relay_set_topic(board_id: str, relay_number: int, *, prefix: str = DEFAULT_TOPIC_PREFIX) -> str:
    """Build the command topic the backend publishes to."""
    return f"{prefix}/{board_id}/relays/{relay_number}/set"


def subscription_topic(*, prefix: str = DEFAULT_TOPIC_PREFIX) -> str:
    """Wildcard subscription for every board message."""
    return f"{prefix}/#"
