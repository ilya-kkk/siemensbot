from datetime import date, timedelta

import pytest

from app.services.admin_views import (
    format_ping_delays,
    parse_offer_url,
    parse_ping_delays,
    ping_delays_from_config,
    render_start_html,
    render_stats_rich_html,
)


def test_parse_offer_url_accepts_absolute_http_urls() -> None:
    assert parse_offer_url(" https://example.com/form?q=1 ") == "https://example.com/form?q=1"
    assert parse_offer_url("http://localhost:8080/path") == "http://localhost:8080/path"


@pytest.mark.parametrize(
    "value",
    [None, "", "example.com", "/relative", "ftp://example.com", "https://", "https://bad port"],
)
def test_parse_offer_url_rejects_non_absolute_http_urls(value: str | None) -> None:
    with pytest.raises(ValueError):
        parse_offer_url(value)


def test_parse_ping_delays_converts_decimal_hours_to_minutes() -> None:
    assert parse_ping_delays("2 24 72") == (120, 1440, 4320)
    assert parse_ping_delays("0.5 1.25 2") == (30, 75, 120)
    assert parse_ping_delays("0,5; 1,25; 2") == (30, 75, 120)
    assert parse_ping_delays("2, 24, 72") == (120, 1440, 4320)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "2 24",
        "zero 24 72",
        "0 24 72",
        "2 2 72",
        "24 2 72",
        "0.001 1 2",
        "1 2 999999999999999999999999999999",
    ],
)
def test_parse_ping_delays_rejects_invalid_values(value: str | None) -> None:
    with pytest.raises(ValueError):
        parse_ping_delays(value)


def test_ping_delay_config_helpers() -> None:
    config = {
        "ping_1_delay_minutes": 120,
        "ping_2_delay_minutes": 1440,
        "ping_3_delay_minutes": 4320,
    }
    assert ping_delays_from_config(config) == (120, 1440, 4320)
    assert format_ping_delays((120, 1440, 4320)) == "2 / 24 / 72 ч (120 / 1440 / 4320 мин)"


def _metrics(seed: int, *, include_cost: bool = False) -> dict[str, object]:
    result: dict[str, object] = {
        "started_users": seed + 10,
        "dialogue_users": seed + 9,
        "in_progress_users": seed + 8,
        "ping_1_sent_users": seed + 7,
        "ping_1_answered_users": seed + 6,
        "ping_2_sent_users": seed + 5,
        "ping_2_answered_users": seed + 4,
        "ping_3_sent_users": seed + 3,
        "ping_3_answered_users": seed + 2,
        "total_leads": seed + 1,
    }
    if include_cost:
        result["ai_cost_usd"] = "0.42"
    return result


def test_render_stats_rich_html_has_open_overall_and_14_closed_cohorts() -> None:
    today = date(2026, 7, 13)
    daily = [
        {
            "date": today - timedelta(days=index),
            "metrics": {**_metrics(index), "ai_cost_usd": "do-not-show"},
        }
        for index in range(16)
    ]

    html = render_stats_rich_html({"overall": _metrics(0, include_cost=True), "daily": daily})

    assert html.count("<details open>") == 1
    assert html.count("<details>") == 14
    assert html.count("</details>") == 15
    assert "За всё время" in html
    assert "13.07.2026" in html
    assert "30.06.2026" in html
    assert "29.06.2026" not in html
    assert "Пинг 1 отправлен" in html
    assert "Ответили на пинг 3" in html
    assert "Нажали кнопку / лиды" in html
    assert html.count("AI cost USD") == 1
    assert "0.42" in html
    assert "do-not-show" not in html


def test_render_start_describes_new_admin_actions() -> None:
    html = render_start_html()

    assert "Установить ссылку" in html
    assert "Настроить пинги" in html
    assert "Отмена" in html
    assert "пинги" in html
    assert "follow-up" not in html.lower()
