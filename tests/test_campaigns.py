from datetime import UTC, datetime, timedelta

import pytest

from app.services.campaigns import batch_count, schedule_batches


def test_schedule_batches_uses_batch_size_and_interval() -> None:
    start = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    assignments = schedule_batches([1, 2, 3, 4, 5], batch_size=2, interval_minutes=30, start_at=start)

    assert [item.batch_number for item in assignments] == [1, 1, 2, 2, 3]
    assert [item.scheduled_at for item in assignments] == [
        start,
        start,
        start + timedelta(minutes=30),
        start + timedelta(minutes=30),
        start + timedelta(minutes=60),
    ]


def test_schedule_batches_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        schedule_batches([1], batch_size=0, interval_minutes=30)
    with pytest.raises(ValueError):
        schedule_batches([1], batch_size=100, interval_minutes=0)


def test_batch_count() -> None:
    assert batch_count(0, 100) == 0
    assert batch_count(300, 100) == 3
    assert batch_count(301, 100) == 4
