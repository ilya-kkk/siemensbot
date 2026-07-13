import re
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html import escape
from typing import Any
from urllib.parse import urlsplit

_PING_METRICS = (
    ("Нажали Start", "started_users"),
    ("Начали диалог", "dialogue_users"),
    ("В процессе диалога", "in_progress_users"),
    ("Пинг 1 отправлен", "ping_1_sent_users"),
    ("Ответили на пинг 1", "ping_1_answered_users"),
    ("Пинг 2 отправлен", "ping_2_sent_users"),
    ("Ответили на пинг 2", "ping_2_answered_users"),
    ("Пинг 3 отправлен", "ping_3_sent_users"),
    ("Ответили на пинг 3", "ping_3_answered_users"),
    ("Нажали кнопку / лиды", "total_leads"),
)
_MAX_POSTGRES_INTEGER = 2_147_483_647


def parse_offer_url(text: str | None) -> str:
    """Return a validated absolute HTTP(S) URL entered by an admin."""
    value = (text or "").strip()
    if not value or any(character.isspace() for character in value):
        raise ValueError("URL must be an absolute HTTP(S) URL")

    try:
        parsed = urlsplit(value)
        # Reading port also validates malformed/non-numeric ports.
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("URL must be an absolute HTTP(S) URL") from exc

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
        raise ValueError("URL must be an absolute HTTP(S) URL")
    return value


def _split_ping_delay_values(text: str | None) -> list[str]:
    value = (text or "").strip()
    if not value:
        raise ValueError("expected three ping delays")

    parts = [part for part in re.split(r"[\s;]+", value) if part]
    # Also accept the familiar `2, 24, 72` and `2,24,72` forms while keeping
    # a comma inside `1,5` available as a decimal separator.
    if len(parts) == 1 and parts[0].count(",") == 2:
        parts = parts[0].split(",")
    elif len(parts) == 3:
        parts = [part[:-1] if part.endswith(",") else part for part in parts]

    if len(parts) != 3 or any(not part for part in parts):
        raise ValueError("expected three ping delays")
    return parts


def parse_ping_delays(text: str | None) -> tuple[int, int, int]:
    """Parse three decimal hour values and convert them to whole minutes."""
    values: list[int] = []
    for part in _split_ping_delay_values(text):
        try:
            hours = Decimal(part.replace(",", "."))
        except InvalidOperation as exc:
            raise ValueError("ping delays must be decimal hours") from exc
        if not hours.is_finite() or hours <= 0:
            raise ValueError("ping delays must be positive")

        minute_value = (hours * 60).to_integral_value(rounding=ROUND_HALF_UP)
        if minute_value > _MAX_POSTGRES_INTEGER:
            raise ValueError("ping delays are too large")
        minutes = int(minute_value)
        if minutes <= 0:
            raise ValueError("ping delays must be at least one minute")
        values.append(minutes)

    if not values[0] < values[1] < values[2]:
        raise ValueError("ping delays must be strictly ascending")
    return values[0], values[1], values[2]


def ping_delays_from_config(config: Mapping[str, Any]) -> tuple[int, int, int]:
    """Read ping delays from the repository's config mapping."""
    keys = (
        "ping_1_delay_minutes",
        "ping_2_delay_minutes",
        "ping_3_delay_minutes",
    )
    if all(key in config for key in keys):
        return tuple(int(config[key]) for key in keys)  # type: ignore[return-value]

    delays = config.get("ping_delays_minutes", config.get("ping_delays"))
    if isinstance(delays, Sequence) and not isinstance(delays, (str, bytes)) and len(delays) == 3:
        return tuple(int(value) for value in delays)  # type: ignore[return-value]
    raise KeyError("config does not contain three ping delays")


def format_ping_delays(delays: Sequence[int]) -> str:
    def hours_text(minutes: int) -> str:
        hours = Decimal(minutes) / Decimal(60)
        rendered = format(hours.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), "f")
        return rendered.rstrip("0").rstrip(".")

    minute_values = " / ".join(str(int(value)) for value in delays)
    hour_values = " / ".join(hours_text(int(value)) for value in delays)
    return f"{hour_values} ч ({minute_values} мин)"


def render_start_html() -> str:
    return "\n".join(
        [
            "<b>Админка</b>",
            "",
            "CSV - скачать таблицу лидов.",
            "Статистика - посмотреть общую статистику и когорты за 14 дней.",
            "Диалог - выгрузить диалог по username или chat_id.",
            "Установить ссылку - изменить адрес финальной кнопки.",
            "Настроить пинги - задать три интервала в часах.",
            "Отмена - выйти из настройки ссылки или пингов.",
            "Стоп - аварийно остановить клиентского бота и пинги.",
        ]
    )


def _metric_table(metrics: Mapping[str, Any], *, include_ai_cost: bool) -> str:
    rows = [
        "<tr><th align=\"left\">Этап</th><th align=\"right\">Пользователи</th></tr>"
    ]
    rows.extend(
        f'<tr><td>{escape(label)}</td><td align="right">{escape(str(metrics.get(key, 0)))}</td></tr>'
        for label, key in _PING_METRICS
    )
    table = "<table bordered striped>" + "".join(rows) + "</table>"
    if include_ai_cost:
        cost = escape(str(metrics.get("ai_cost_usd", 0)))
        table += f"<p><b>AI cost USD:</b> <code>{cost}</code></p>"
    return table


def _format_cohort_date(value: Any) -> str:
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    try:
        return date.fromisoformat(str(value)).strftime("%d.%m.%Y")
    except ValueError:
        return str(value)


def render_stats_rich_html(data: Mapping[str, Any]) -> str:
    """Render overall and up to 14 daily cohorts as Telegram Rich HTML."""
    overall = data.get("overall")
    if not isinstance(overall, Mapping):
        raise ValueError("stats must contain overall metrics")

    blocks = [
        "<h1>Статистика воронки</h1>",
        "<details open><summary>За всё время</summary>",
        _metric_table(overall, include_ai_cost=True),
        "</details>",
    ]
    daily = data.get("daily") or []
    for cohort in list(daily)[:14]:
        if not isinstance(cohort, Mapping):
            continue
        cohort_date = escape(_format_cohort_date(cohort.get("date", "")))
        metrics = cohort.get("metrics", cohort)
        if not isinstance(metrics, Mapping):
            continue
        blocks.extend(
            [
                f"<details><summary>{cohort_date}</summary>",
                _metric_table(metrics, include_ai_cost=False),
                "</details>",
            ]
        )
    return "\n".join(blocks)


def render_stats_html(data: Mapping[str, Any]) -> str:
    """Backward-compatible name for callers migrating to rich messages."""
    return render_stats_rich_html(data)
