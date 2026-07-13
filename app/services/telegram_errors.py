from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TelegramErrorAction:
    recipient_status: str
    user_status: str | None
    retry_after_seconds: int | None = None
    is_critical: bool = False


def classify_telegram_error(
    status_code: int,
    description: str,
    parameters: dict[str, Any] | None = None,
) -> TelegramErrorAction:
    """Map Bot API failures to retry or terminal user actions.

    A generic 400 is deliberately not terminal: malformed text or markup is a
    worker/application problem and must not silently invalidate the user.
    """

    text = description.lower()
    parameters = parameters or {}

    if status_code == 401 or "unauthorized" in text:
        return TelegramErrorAction("failed", None, is_critical=True)

    if status_code == 429:
        raw_retry_after = parameters.get("retry_after", 60)
        try:
            retry_after = max(1, int(raw_retry_after))
        except (TypeError, ValueError):
            retry_after = 60
        return TelegramErrorAction("rescheduled", None, retry_after_seconds=retry_after)

    invalid_markers = (
        "chat not found",
        "user not found",
        "user is deactivated",
        "chat_id is empty",
        "bot can't initiate conversation",
        "peer_id_invalid",
    )
    if status_code == 404 or (
        status_code == 400 and any(marker in text for marker in invalid_markers)
    ):
        return TelegramErrorAction("failed", "invalid")

    if status_code == 403:
        return TelegramErrorAction("failed", "blocked")

    return TelegramErrorAction("failed", None, is_critical=status_code == 400)
