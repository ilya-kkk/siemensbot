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
        unanswered_users=[
            {
                "user_record_id": 11,
                "telegram_user_id": 321,
                "chat_id": 654,
                "username": "silent",
                "telegram_name": "Молчун <script>",
                "started_at": datetime(2026, 7, 20, 7, 30, tzinfo=UTC),
                "status": "active",
            }
        ],
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
    assert "без ответа 1" in html
    assert html.index("Не ответили на первое сообщение") < html.index("Диалог #1")
    assert "Молчун &lt;script&gt; · @silent" in html
    assert "сформирован 20.07.2026 13:30 MSK" in html
    assert 'data-dialogue-id="dialogue-10"' in html
    assert 'name="lead_status-1"' in html
    assert 'name="response_acceptable-1"' in html
    assert 'name="button_should_be_shown_now-1"' in html
    assert 'name="failure_tags" value="wrong_next_step"' in html
    assert '<textarea name="expected_behavior"' in html
    assert '<textarea name="suggested_response"' in html
    assert '<textarea name="expert_note"' in html
    assert "localStorage" in html
    assert "dialogue-review-v1" in html
    assert "Скачать reviewed.json" in html
    assert 'JSON.stringify(artifact, null, 2) + "\\n"' in html


def test_render_dialogues_report_has_empty_state() -> None:
    html = render_dialogues_report_html([]).decode("utf-8")

    assert "Диалогов пока нет" in html
    assert "0 диалогов · 0 сообщений" in html
    assert "0 пользователей" in html
    assert "Оценено 0 из 0" in html
