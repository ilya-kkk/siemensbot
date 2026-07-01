from html import escape

MAX_TELEGRAM_HTML_CHARS = 3900


def render_dialogue_html(messages: list[dict]) -> str:
    parts: list[str] = []
    for message in messages:
        direction = escape(str(message.get("direction", "")))
        created_at = escape(str(message.get("created_at", "")))
        text = escape(message.get("text") or "")
        parts.append(f"<b>{created_at} {direction}</b>\n{text}")
    return "\n\n".join(parts) or "<i>Диалог пуст</i>"


def split_telegram_html(text: str, limit: int = MAX_TELEGRAM_HTML_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for block in text.split("\n\n"):
        extra = len(block) + (2 if current else 0)
        if current and current_len + extra > limit:
            chunks.append("\n\n".join(current))
            current = [block]
            current_len = len(block)
        else:
            current.append(block)
            current_len += extra
    if current:
        chunks.append("\n\n".join(current))
    return chunks
