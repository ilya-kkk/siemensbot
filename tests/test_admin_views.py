import pytest

from app.services.admin_views import parse_campaign_settings, render_campaign_html


def test_parse_campaign_settings_accepts_comma_and_spaces() -> None:
    assert parse_campaign_settings("100, 30") == (100, 30)
    assert parse_campaign_settings("100 30") == (100, 30)


def test_parse_campaign_settings_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        parse_campaign_settings("100")
    with pytest.raises(ValueError):
        parse_campaign_settings("0, 30")


def test_render_campaign_html_hides_technical_id() -> None:
    html = render_campaign_html(
        {
            "id": 999,
            "status": "running",
            "batch_size": 100,
            "interval_minutes": 30,
            "total_recipients": 200,
            "sent": 20,
            "pending": 180,
            "failed": 0,
        },
        client_bot_stopped=False,
    )

    assert "ID" not in html
    assert "999" not in html
    assert "Получателей всего" not in html
    assert "Отправлено" not in html
    assert "В очереди" not in html
    assert "Ошибки" not in html
