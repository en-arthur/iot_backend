from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.batching import ReadingBatcher, ReadingRow

BASE = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


class TestLatestPerSensor:
    def test_keeps_newest_reading_per_sensor(self):
        rows = [
            ReadingRow("b1", "temp", 1.0, BASE),
            ReadingRow("b1", "temp", 2.0, BASE + timedelta(seconds=1)),
            ReadingRow("b1", "temp", 1.5, BASE + timedelta(seconds=2)),
            ReadingRow("b1", "hum", 40.0, BASE),
            ReadingRow("b2", "temp", 9.0, BASE),
        ]

        latest = ReadingBatcher._latest_per_sensor(rows)

        assert len(latest) == 3
        assert latest[("b1", "temp")]["value"] == 1.5
        assert latest[("b1", "hum")]["value"] == 40.0
        assert latest[("b2", "temp")]["value"] == 9.0

    def test_empty_batch(self):
        assert ReadingBatcher._latest_per_sensor([]) == {}
