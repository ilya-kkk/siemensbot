from datetime import UTC, date, datetime, timedelta

import pytest

from app.services.admin_views import (
    format_ping_delays,
    parse_growth_alert_threshold,
    parse_ping_delays,
    ping_delays_from_config,
    render_admin_summary_html,
    render_start_html,
    render_stats_rich_html,
    render_users_rich_html,
)


def test_render_admin_summary_uses_compact_format_and_moscow_time() -> None:
    html = render_admin_summary_html(
        {"start_24h": 12, "lead_24h": 3, "start_all": 148, "lead_all": 27},
        datetime(2026, 7, 17, 15, 40, tzinfo=UTC),
    )

    assert html.splitlines() == [
        "start: 12 | lead: 3 | 24h",
        "start: 148 | lead: 27 | all",
        "Обновлено: 17.07.2026 18:40 MSK",
    ]


def test_render_admin_summary_marks_unavailable_values() -> None:
    assert render_admin_summary_html(None, None).splitlines() == [
        "start: — | lead: — | 24h",
        "start: — | lead: — | all",
        "Обновлено: недоступно",
    ]


def test_parse_growth_alert_threshold_accepts_positive_bigint() -> None:
    assert parse_growth_alert_threshold(" 100 ") == 100
    assert parse_growth_alert_threshold("9223372036854775807") == 9_223_372_036_854_775_807


@pytest.mark.parametrize(
    "value",
    [None, "", "0", "-1", "+1", "1.5", "1,5", "one", "１２", "9223372036854775808"],
)
def test_parse_growth_alert_threshold_rejects_invalid_values(value: str | None) -> None:
    with pytest.raises(ValueError):
        parse_growth_alert_threshold(value)


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

    assert "Установить ссылку" not in html
    assert "Настроить пинги" in html
    assert "Установить алерт" in html
    assert "Юзеры" in html
    assert "Отмена" in html
    assert "пинги" in html
    assert "follow-up" not in html.lower()


def test_render_users_rich_html_links_users_and_renders_empty_days() -> None:
    data = {
        "daily": [
            {
                "date": date(2026, 7, 18),
                "count": 1,
                "users": [
                    {
                        "username": "<admin>",
                        "telegram_user_id": 1,
                        "user_record_id": 10,
                    }
                ],
            },
            {
                "date": date(2026, 7, 17),
                "count": 2,
                "users": [
                    {
                        "username": None,
                        "telegram_user_id": 123456,
                        "user_record_id": 11,
                    },
                    {
                        "username": None,
                        "telegram_user_id": None,
                        "user_record_id": 12,
                    },
                ],
            },
            {
                "date": date(2026, 7, 16),
                "count": 5,
                "users": [
                    {"username": f"user{index}", "user_record_id": 20 + index}
                    for index in range(5)
                ],
            },
            {"date": date(2026, 7, 15), "count": 0, "users": []},
        ]
    }

    messages = render_users_rich_html(data)

    assert len(messages) == 1
    html = messages[0]
    assert html.startswith("<h1>Юзеры</h1>")
    assert html.count("<details>") == 4
    assert "<details open>" not in html
    assert "18.07.2026 — 1 пользователь" in html
    assert "17.07.2026 — 2 пользователя" in html
    assert "16.07.2026 — 5 пользователей" in html
    assert "15.07.2026 — 0 пользователей" in html
    assert '<a href="https://t.me/%3Cadmin%3E">@&lt;admin&gt;</a>' in html
    assert '<a href="tg://user?id=123456">ID 123456</a>' in html
    assert "ID недоступен" in html
    assert "@&lt;admin&gt;</a> · /dialog_10" in html
    assert "ID 123456</a> · /dialog_11" in html
    assert "ID недоступен · /dialog_12" in html
    assert "<p>Нет пользователей</p>" in html


def test_render_users_rich_html_splits_large_days_without_losing_users() -> None:
    users = [
        {"username": f"user{index:04d}", "user_record_id": index + 1}
        for index in range(1000)
    ]

    messages = render_users_rich_html(
        {"daily": [{"date": date(2026, 7, 18), "count": len(users), "users": users}]}
    )

    assert len(messages) > 1
    combined = "\n".join(messages)
    assert combined.count("<li>") == len(users)
    assert "часть" in combined
    for index in range(1000):
        assert combined.count(f">@user{index:04d}</a>") == 1
        assert combined.count(f"/dialog_{index + 1}<") == 1
    for html in messages:
        assert len(html.encode("utf-8")) <= 29_000
        estimated_blocks = 1 + (2 * html.count("<details>")) + html.count("<li>")
        assert estimated_blocks <= 440
