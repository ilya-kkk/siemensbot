import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html import escape
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

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
_MAX_POSTGRES_BIGINT = 9_223_372_036_854_775_807
_MOSCOW_TZ = ZoneInfo("Europe/Moscow")
_RICH_MESSAGE_MAX_BYTES = 29_000
_RICH_MESSAGE_MAX_BLOCKS = 440
_USERS_TITLE_PLACEHOLDER = "<h1>Юзеры · часть 9999/9999</h1>"


def render_admin_summary_html(
    metrics: Mapping[str, Any] | None,
    updated_at: datetime | None,
) -> str:
    """Render the compact summary prepended to both pinned admin messages."""
    values = metrics or {}

    def metric(key: str) -> str:
        value = values.get(key)
        return escape(str(value)) if value is not None else "—"

    updated_text = (
        updated_at.astimezone(_MOSCOW_TZ).strftime("%d.%m.%Y %H:%M MSK")
        if updated_at is not None
        else "недоступно"
    )
    return "\n".join(
        [
            f"start: {metric('start_24h')} | lead: {metric('lead_24h')} | 24h",
            f"start: {metric('start_all')} | lead: {metric('lead_all')} | all",
            f"Обновлено: {updated_text}",
        ]
    )


def parse_growth_alert_threshold(text: str | None) -> int:
    """Parse a positive PostgreSQL bigint without accepting signs or decimals."""
    value = (text or "").strip()
    if not value or not value.isascii() or not value.isdigit():
        raise ValueError("growth alert threshold must be a positive integer")
    threshold = int(value)
    if threshold <= 0 or threshold > _MAX_POSTGRES_BIGINT:
        raise ValueError("growth alert threshold is outside the bigint range")
    return threshold


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
            "Таблица - скачать Excel-таблицу лидов.",
            "Статистика - посмотреть общую статистику и когорты за 14 дней.",
            "Юзеры - посмотреть пользователей по дням за последние 14 дней.",
            "Сгенерировать линк - создать ссылку с источником для канала.",
            "Диалог - выгрузить диалог по username или chat_id.",
            "Настроить пинги - задать три интервала в часах.",
            "Установить алерт - уведомить обоих админов после заданного числа новых пользователей.",
            "Отмена - выйти из текущей настройки.",
            "Стоп - аварийно остановить клиентского бота и пинги.",
        ]
    )


def parse_referral_source_title(text: str | None) -> str:
    title = (text or "").strip()
    if not title:
        raise ValueError("referral source title is required")
    if len(title) > 200:
        raise ValueError("referral source title is too long")
    return title


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


def _users_count_text(count: int) -> str:
    remainder_100 = count % 100
    remainder_10 = count % 10
    if 11 <= remainder_100 <= 14:
        word = "пользователей"
    elif remainder_10 == 1:
        word = "пользователь"
    elif 2 <= remainder_10 <= 4:
        word = "пользователя"
    else:
        word = "пользователей"
    return f"{count} {word}"


def _user_link(user: Mapping[str, Any]) -> str:
    username = str(user.get("username") or "").strip().lstrip("@")
    if username:
        href = f"https://t.me/{quote(username, safe='')}"
        return f'<a href="{escape(href, quote=True)}">@{escape(username)}</a>'

    telegram_user_id = user.get("telegram_user_id")
    try:
        numeric_id = int(telegram_user_id) if telegram_user_id is not None else None
    except (TypeError, ValueError):
        numeric_id = None
    if numeric_id is not None and numeric_id > 0:
        return f'<a href="tg://user?id={numeric_id}">ID {numeric_id}</a>'
    return "ID недоступен"


def _user_row(user: Mapping[str, Any]) -> str:
    profile_link = _user_link(user)
    user_record_id = user.get("user_record_id")
    try:
        numeric_id = int(user_record_id) if user_record_id is not None else None
    except (TypeError, ValueError):
        numeric_id = None
    if numeric_id is None or numeric_id <= 0:
        return profile_link
    return f"{profile_link} · /dialog_{numeric_id}"


def _users_day_block(
    cohort_date: Any,
    total_count: int,
    user_links: Sequence[str],
    *,
    part: int | None = None,
    part_count: int | None = None,
) -> str:
    date_label = escape(_format_cohort_date(cohort_date))
    summary = f"{date_label} — {_users_count_text(total_count)}"
    if part is not None and part_count is not None:
        summary += f" (часть {part}/{part_count})"
    if user_links:
        body = "<ul>" + "".join(f"<li>{link}</li>" for link in user_links) + "</ul>"
    else:
        body = "<p>Нет пользователей</p>"
    return f"<details><summary>{summary}</summary>{body}</details>"


def _users_day_block_count(user_count: int) -> int:
    # One details block, one list/paragraph block, and one block per list item.
    return 2 + user_count


def _rich_message_fits(html: str, block_count: int) -> bool:
    return (
        len(html.encode("utf-8")) <= _RICH_MESSAGE_MAX_BYTES
        and block_count <= _RICH_MESSAGE_MAX_BLOCKS
    )


def _split_users_day(
    cohort_date: Any,
    total_count: int,
    user_links: Sequence[str],
) -> list[tuple[str, int]]:
    full_block = _users_day_block(cohort_date, total_count, user_links)
    full_block_count = _users_day_block_count(len(user_links))
    if _rich_message_fits(
        f"{_USERS_TITLE_PLACEHOLDER}\n{full_block}",
        1 + full_block_count,
    ):
        return [(full_block, full_block_count)]

    groups: list[list[str]] = []
    current: list[str] = []
    for link in user_links:
        candidate = [*current, link]
        candidate_block = _users_day_block(
            cohort_date,
            total_count,
            candidate,
            part=9999,
            part_count=9999,
        )
        candidate_count = _users_day_block_count(len(candidate))
        if current and not _rich_message_fits(
            f"{_USERS_TITLE_PLACEHOLDER}\n{candidate_block}",
            1 + candidate_count,
        ):
            groups.append(current)
            current = [link]
        else:
            current = candidate
    if current:
        groups.append(current)

    part_count = len(groups)
    return [
        (
            _users_day_block(
                cohort_date,
                total_count,
                group,
                part=index,
                part_count=part_count,
            ),
            _users_day_block_count(len(group)),
        )
        for index, group in enumerate(groups, start=1)
    ]


def render_users_rich_html(data: Mapping[str, Any]) -> list[str]:
    """Render daily first-start cohorts, splitting safely across rich messages."""
    day_parts: list[tuple[str, int]] = []
    daily = data.get("daily") or []
    if isinstance(daily, Sequence) and not isinstance(daily, (str, bytes)):
        for cohort in daily:
            if not isinstance(cohort, Mapping):
                continue
            users = cohort.get("users") or []
            if not isinstance(users, Sequence) or isinstance(users, (str, bytes)):
                users = []
            user_links = [_user_row(user) for user in users if isinstance(user, Mapping)]
            if not user_links:
                continue
            try:
                total_count = int(cohort.get("count", len(user_links)))
            except (TypeError, ValueError):
                total_count = len(user_links)
            total_count = max(total_count, len(user_links), 0)
            day_parts.extend(
                _split_users_day(cohort.get("date", ""), total_count, user_links)
            )

    if not day_parts:
        return ["<h1>Юзеры</h1>\n<p>За последние 14 дней пользователей нет</p>"]

    message_parts: list[list[tuple[str, int]]] = []
    current: list[tuple[str, int]] = []
    for day_part in day_parts:
        candidate = [*current, day_part]
        candidate_html = "\n".join(
            [_USERS_TITLE_PLACEHOLDER, *(block for block, _count in candidate)]
        )
        candidate_blocks = 1 + sum(count for _block, count in candidate)
        if current and not _rich_message_fits(candidate_html, candidate_blocks):
            message_parts.append(current)
            current = [day_part]
        else:
            current = candidate
    if current or not message_parts:
        message_parts.append(current)

    message_count = len(message_parts)
    messages: list[str] = []
    for index, parts in enumerate(message_parts, start=1):
        title = (
            "<h1>Юзеры</h1>"
            if message_count == 1
            else f"<h1>Юзеры · часть {index}/{message_count}</h1>"
        )
        messages.append("\n".join([title, *(block for block, _count in parts)]))
    return messages
