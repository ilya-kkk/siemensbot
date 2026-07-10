from datetime import UTC, datetime

from app.services.transcript import render_dialogue_html, split_telegram_html


def test_render_dialogue_html_is_compact_and_escapes_user_text() -> None:
    html = render_dialogue_html(
        [
            {
                "created_at": datetime(2026, 7, 1, 12, 5, 59, 123456, tzinfo=UTC),
                "direction": "incoming",
                "telegram_name": "Илья КК",
                "text": "<script>alert(1)</script>",
            },
            {
                "created_at": "2026-07-01T12:06:00+00:00",
                "direction": "outgoing",
                "telegram_name": "Илья КК",
                "text": "Ответ бота",
            }
        ]
    )

    assert "&lt;script&gt;" in html
    assert "<script>" not in html
    assert "<b>01.07.2026</b>" in html
    assert "<b>Илья КК | 15:05</b>" in html
    assert "<b>Бот | 15:06</b>" in html
    assert "Сообщение 1" not in html
    assert "Пользователь" not in html
    assert "+00:00" not in html
    assert "123456" not in html
    assert "\n\n" not in html


def test_split_telegram_html_keeps_chunks_under_limit() -> None:
    text = "\n\n".join(f"block {i} " + "x" * 20 for i in range(20))
    chunks = split_telegram_html(text, limit=100)

    assert len(chunks) > 1
    assert all(len(chunk) <= 100 for chunk in chunks)
