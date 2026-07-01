from app.services.telegram_errors import classify_telegram_error


def test_403_marks_user_blocked() -> None:
    action = classify_telegram_error(403, "Forbidden: bot was blocked by the user")

    assert action.recipient_status == "failed"
    assert action.user_status == "blocked"
    assert not action.is_critical


def test_429_reschedules_with_retry_after() -> None:
    action = classify_telegram_error(429, "Too Many Requests", {"retry_after": 42})

    assert action.recipient_status == "rescheduled"
    assert action.retry_after_seconds == 42


def test_unauthorized_is_critical() -> None:
    action = classify_telegram_error(401, "Unauthorized")

    assert action.is_critical
