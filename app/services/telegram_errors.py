from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TelegramErrorAction:
    recipient_status: str
    user_status: str | None
    retry_after_seconds: int | None = None
    is_critical: bool = False


def classify_telegram_error(status_code: int, description: str, parameters: dict[str, Any] | None = None) -> TelegramErrorAction:
    text = description.lower()
    parameters = parameters or {}

    if status_code == 401 or "unauthorized" in text or "token" in text:
        return TelegramErrorAction("failed", None, is_critical=True)

    if status_code == 403:
        return TelegramErrorAction("failed", "blocked")

    if status_code == 429:
        retry_after = parameters.get("retry_after")
        return TelegramErrorAction("rescheduled", None, int(retry_after) if retry_after else 60)

    invalid_markers = (
        "chat not found",
        "user not found",
        "chat_id is empty",
        "bad request: chat",
        "bot can't initiate conversation",
    )
    if status_code == 400 and any(marker in text for marker in invalid_markers):
        return TelegramErrorAction("failed", "invalid")

    return TelegramErrorAction("failed", None)
