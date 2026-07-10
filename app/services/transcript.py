import re
from datetime import UTC, datetime, timedelta, timezone
from html import escape
from typing import Any

MAX_TELEGRAM_HTML_CHARS = 3900
MOSCOW_TZ = timezone(timedelta(hours=3))


def _as_moscow_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(MOSCOW_TZ)


def _date_label(value: Any) -> str:
    dt = _as_moscow_datetime(value)
    return dt.strftime("%d.%m.%Y") if dt else str(value or "")


def _time_label(value: Any) -> str:
    dt = _as_moscow_datetime(value)
    return dt.strftime("%H:%M") if dt else str(value or "")


def _sender_label(message: dict) -> str:
    direction = str(message.get("direction") or "")
    if direction == "outgoing":
        return "Бот"
    if direction == "system":
        return "Система"

    telegram_name = str(message.get("telegram_name") or "").strip()
    if telegram_name:
        return telegram_name

    username = str(message.get("username") or "").strip().lstrip("@")
    return f"@{username}" if username else "Пользователь"


def render_dialogue_html(messages: list[dict]) -> str:
    if not messages:
        return "<b>Диалог</b>\n\n<i>Сообщений нет</i>"

    parts: list[str] = ["<b>Диалог</b>"]
    current_date = ""
    for message in messages:
        date_label = _date_label(message.get("created_at"))
        if date_label and date_label != current_date:
            parts.append(f"<b>{escape(date_label)}</b>")
            current_date = date_label

        sender = escape(_sender_label(message))
        time = escape(_time_label(message.get("created_at")))
        text = escape(message.get("text") or "<пустое сообщение>")
        parts.append(
            f"<blockquote><b>{sender} | {time}</b>\n{text}</blockquote>"
        )
    return "\n".join(parts)


def split_telegram_html(text: str, limit: int = MAX_TELEGRAM_HTML_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    blocks = re.split(r"(?<=</blockquote>)\n", text) if "</blockquote>" in text else text.split("\n\n")
    separator = "\n" if "</blockquote>" in text else "\n\n"
    for block in blocks:
        extra = len(block) + (len(separator) if current else 0)
        if current and current_len + extra > limit:
            chunks.append(separator.join(current))
            current = [block]
            current_len = len(block)
        else:
            current.append(block)
            current_len += extra
    if current:
        chunks.append(separator.join(current))
    return chunks
