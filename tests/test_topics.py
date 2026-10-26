from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.topics import (
    is_command_topic,
    parse_relay_payload,
    parse_sensor_payload,
    parse_status_payload,
    parse_topic,
    relay_set_topic,
    subscription_topic,
)

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


class TestParseTopic:
    def test_status(self):
        parsed = parse_topic("boards/esp32-01/status")
        assert parsed is not None
        assert parsed.kind == "status"
        assert parsed.board_id == "esp32-01"
        assert parsed.sensor is None
        assert parsed.relay_number is None

    def test_sensor(self):
        parsed = parse_topic("boards/esp32-01/sensors/temperature")
        assert parsed is not None
        assert parsed.kind == "sensor"
        assert parsed.board_id == "esp32-01"
        assert parsed.sensor == "temperature"

    def test_relay_state(self):
        parsed = parse_topic("boards/esp32-01/relays/3/state")
        assert parsed is not None
        assert parsed.kind == "relay_state"
        assert parsed.relay_number == 3

    def test_relay_set_is_not_inbound(self):
        # The backend publishes to /set; it must not treat it as a report.
        assert parse_topic("boards/esp32-01/relays/1/set") is None

    @pytest.mark.parametrize(
        "topic",
        [
            "",
            "boards",
            "boards/",
            "boards//status",
            "boards/esp32-01",
            "boards/esp32-01/status/extra",
            "boards/esp32-01/sensors",
            "boards/esp32-01/sensors/",
            "boards/esp32-01/sensors/a/b",
            "boards/esp32-01/relays/state",
            "boards/esp32-01/relays//state",
            "boards/esp32-01/relays/0/state",
            "boards/esp32-01/relays/-1/state",
            "boards/esp32-01/relays/1000/state",
            "boards/esp32-01/relays/1.5/state",
            "boards/esp32 01/status",
            "boards/esp32-01/STATUS",
            "other/esp32-01/status",
            "boards/esp32-01/relay/1/state",
        ],
    )
    def test_malformed_topics_return_none(self, topic):
        assert parse_topic(topic) is None

    def test_custom_prefix(self):
        parsed = parse_topic("devices/board-1/status", prefix="devices")
        assert parsed is not None
        assert parsed.board_id == "board-1"
        assert parse_topic("boards/board-1/status", prefix="devices") is None

    def test_builders(self):
        assert relay_set_topic("b1", 2) == "boards/b1/relays/2/set"
        assert subscription_topic() == "boards/#"


class TestStatusPayload:
    @pytest.mark.parametrize("payload", ["online", "OFFLINE", b" online\n", "Online"])
    def test_valid(self, payload):
        assert parse_status_payload(payload) in {"online", "offline"}

    @pytest.mark.parametrize("payload", ["", "on", "true", b"\xff\xfe", "unknown"])
    def test_invalid(self, payload):
        assert parse_status_payload(payload) is None


class TestRelayPayload:
    @pytest.mark.parametrize(
        ("payload", "expected"),
        [("on", "on"), (b"OFF", "off"), (" on \n", "on")],
    )
    def test_valid(self, payload, expected):
        assert parse_relay_payload(payload) == expected

    @pytest.mark.parametrize("payload", ["", "1", "true", b"\xff", "online"])
    def test_invalid(self, payload):
        assert parse_relay_payload(payload) is None


class TestSensorPayload:
    def test_unix_seconds(self):
        reading = parse_sensor_payload('{"value": 24.5, "ts": 1700000000}', now=NOW)
        assert reading is not None
        assert reading.value == 24.5
        assert reading.ts == datetime.fromtimestamp(1700000000, tz=UTC)

    def test_unix_milliseconds(self):
        reading = parse_sensor_payload('{"value": 1, "ts": 1700000000123}', now=NOW)
        assert reading is not None
        assert reading.ts == datetime.fromtimestamp(1700000000.123, tz=UTC)

    def test_iso_timestamp(self):
        reading = parse_sensor_payload('{"value": 1, "ts": "2026-10-26T10:00:00Z"}', now=NOW)
        assert reading is not None
        assert reading.ts == datetime(2026, 10, 26, 10, 0, tzinfo=UTC)

    def test_naive_iso_is_utc(self):
        reading = parse_sensor_payload('{"value": 1, "ts": "2026-10-26T10:00:00"}', now=NOW)
        assert reading is not None
        assert reading.ts == datetime(2026, 10, 26, 10, 0, tzinfo=UTC)

    def test_missing_ts_uses_receive_time(self):
        reading = parse_sensor_payload('{"value": 3}', now=NOW)
        assert reading is not None
        assert reading.ts == NOW

    def test_integer_value(self):
        reading = parse_sensor_payload(b'{"value": 42, "ts": 1700000000}', now=NOW)
        assert reading is not None
        assert reading.value == 42.0

    def test_implausible_ts_falls_back_to_receive_time(self):
        reading = parse_sensor_payload('{"value": 1, "ts": 12345}', now=NOW)
        assert reading is not None
        assert reading.ts == NOW

    def test_future_ts_falls_back(self):
        future = (NOW + timedelta(days=30)).timestamp()
        reading = parse_sensor_payload(f'{{"value": 1, "ts": {future}}}', now=NOW)
        assert reading is not None
        assert reading.ts == NOW

    @pytest.mark.parametrize(
        "payload",
        [
            "",
            "not json",
            "[1, 2, 3]",
            '"24.5"',
            '{"value": "24.5"}',
            '{"value": true}',
            '{"value": null}',
            '{"value": NaN}',
            '{"value": Infinity}',
            '{"ts": 1700000000}',
            b"\xff\xfe",
        ],
    )
    def test_invalid(self, payload):
        assert parse_sensor_payload(payload, now=NOW) is None


class TestIsCommandTopic:
    def test_relay_set_is_a_command(self):
        assert is_command_topic("boards/esp32-01/relays/1/set") is True

    @pytest.mark.parametrize(
        "topic",
        [
            "boards/esp32-01/status",
            "boards/esp32-01/sensors/temperature",
            "boards/esp32-01/relays/1/state",
            "boards/esp32-01/relays/0/set",
            "boards/esp32-01/relays/x/set",
            "boards/esp32-01/relays/1/set/extra",
            "other/esp32-01/relays/1/set",
            "",
        ],
    )
    def test_other_topics_are_not_commands(self, topic):
        assert is_command_topic(topic) is False
