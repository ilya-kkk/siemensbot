from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil


@dataclass(frozen=True)
class RecipientAssignment:
    telegram_user_id: int
    batch_number: int
    scheduled_at: datetime


def schedule_batches(
    user_ids: list[int],
    batch_size: int,
    interval_minutes: int,
    start_at: datetime | None = None,
) -> list[RecipientAssignment]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")
    base = start_at or datetime.now(UTC)

    assignments: list[RecipientAssignment] = []
    for index, user_id in enumerate(user_ids):
        batch_number = index // batch_size + 1
        scheduled_at = base + timedelta(minutes=interval_minutes * (batch_number - 1))
        assignments.append(
            RecipientAssignment(
                telegram_user_id=user_id,
                batch_number=batch_number,
                scheduled_at=scheduled_at,
            )
        )
    return assignments


def batch_count(total_users: int, batch_size: int) -> int:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    return ceil(total_users / batch_size) if total_users else 0
