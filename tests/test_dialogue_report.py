from datetime import UTC, datetime

from app.services.dialogue_report import render_dialogues_report_html


def test_render_dialogues_report_is_self_contained_chat_html() -> None:
    dialogues = [
        {
            "user_record_id": 10,
            "telegram_user_id": 123,
            "chat_id": 456,
            "username": "client",
            "telegram_name": "Анна <script>",
            "dialogue_started_at": datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
            "funnel_stage": "lead",
            "messages": [
                {
                    "created_at": datetime(2026, 7, 20, 8, 1, tzinfo=UTC),
                    "direction": "incoming",
                    "message_type": "text",
                    "text": "Привет <b>бот</b>",
                },
                {
                    "created_at": datetime(2026, 7, 20, 8, 2, tzinfo=UTC),
                    "direction": "outgoing",
                    "message_type": "text",
                    "text": "Привет, Анна",
                },
            ],
        }
    ]

    report = render_dialogues_report_html(
        dialogues,
        generated_at=datetime(2026, 7, 20, 10, 30, tzinfo=UTC),
    )
    html = report.decode("utf-8")

    assert html.startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in html
    assert "Анна &lt;script&gt; · @client" in html
    assert "Анна <script>" not in html
    assert "Привет &lt;b&gt;бот&lt;/b&gt;" in html
    assert 'class="message-row incoming"' in html
    assert 'class="message-row outgoing"' in html
    assert "начат 20.07.2026 11:00 MSK" in html
    assert "1 диалогов · 2 сообщений" in html
    assert "сформирован 20.07.2026 13:30 MSK" in html


def test_render_dialogues_report_has_empty_state() -> None:
    html = render_dialogues_report_html([]).decode("utf-8")

    assert "Диалогов пока нет" in html
    assert "0 диалогов · 0 сообщений" in html
