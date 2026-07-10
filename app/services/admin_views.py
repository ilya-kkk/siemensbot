import re
from html import escape
from typing import Any


def parse_campaign_settings(text: str | None) -> tuple[int, int]:
    parts = [part for part in re.split(r"[\s,;]+", text or "") if part]
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError("expected batch size and interval")
    batch_size, interval_minutes = map(int, parts)
    if batch_size <= 0 or interval_minutes <= 0:
        raise ValueError("settings must be positive")
    return batch_size, interval_minutes


def _row(label: str, value: Any) -> str:
    return f"{label:<34} {value:>8}"


def _table(rows: list[tuple[str, Any]]) -> str:
    return "<pre>" + escape("\n".join(_row(label, value) for label, value in rows)) + "</pre>"


def render_start_html() -> str:
    return "\n".join(
        [
            "<b>Админка</b>",
            "",
            "Для того, чтобы импортировать старых юзеров, нажмите /import.",
            "",
            "/leads - скачать CSV-таблицу лидов.",
            "/stats - посмотреть HTML-статистику воронки.",
            "/campaign - статус, настройка, пауза и возобновление кампании.",
            "/dialog - выгрузить диалог по username или chat_id.",
            "/stop - аварийно остановить клиентского бота и follow-up.",
        ]
    )


def render_stats_html(data: dict[str, Any]) -> str:
    delivery_errors = data.get("delivery_errors") or []
    error_rows = [
        ("Бот заблокирован", data["blocked_users"]),
        ("Неверный chat_id/user", data["invalid_users"]),
        ("Username без chat_id", data["unresolved_users"]),
        ("Ошибки отправки", data["failed"]),
    ]
    for error in delivery_errors[:8]:
        code = error.get("telegram_error_code") or "unknown"
        error_rows.append((f"Telegram {code}", error.get("count", 0)))

    return "\n\n".join(
        [
            "<b>Статистика воронки</b>",
            _table(
                [
                    ("Старые пользователи", data["old_users"]),
                    ("Активные старые с chat_id", data["active_old_users"]),
                    ("Follow-up запланирован", data["total_recipients"]),
                    ("Follow-up отправлен", data["sent"]),
                    ("В очереди", data["pending"]),
                    ("Ошибки", data["failed"]),
                ]
            ),
            "<b>Ошибки доставки</b>",
            _table(error_rows),
            "<b>Состояния после follow-up</b>",
            _table(
                [
                    ("Отправлен, ответа нет", data["sent_no_reply"]),
                    ("Вступили в диалог", data["replied_users"]),
                    ("Диалог без записи", data["replied_no_lead"]),
                    ("Дошли до кнопки без записи", data["offer_no_lead"]),
                    ("Лиды из follow-up", data["leads_from_followup"]),
                    ("Лиды всего", data["total_leads"]),
                ]
            ),
            _table(
                [
                    ("Клики старых ссылок", data["button_clicks"]),
                    ("AI cost USD", data["ai_cost_usd"]),
                ]
            ),
            "<i>Telegram не отдаёт боту статус прочтения. Поэтому “ответа нет” означает: follow-up отправлен, входящих сообщений после него нет.</i>",
        ]
    )


def render_campaign_html(campaign: dict[str, Any] | None, client_bot_stopped: bool) -> str:
    stop_line = "\n<b>Аварийная остановка клиентского бота включена.</b>" if client_bot_stopped else ""
    if not campaign:
        return (
            "<b>Кампания</b>\n"
            "Активной кампании нет.\n\n"
            "Нажмите “Настройка” и пришлите размер батча и интервал одним сообщением: <code>100, 30</code>."
            f"{stop_line}"
        )

    status_map = {
        "running": "работает",
        "paused": "на паузе",
    }
    status = status_map.get(str(campaign["status"]), campaign["status"])
    return "\n\n".join(
        [
            "<b>Кампания</b>",
            _table(
                [
                    ("ID", campaign["id"]),
                    ("Статус", status),
                    ("Размер батча", campaign["batch_size"]),
                    ("Интервал, минут", campaign["interval_minutes"]),
                    ("Получателей всего", campaign["total_recipients"]),
                    ("Отправлено", campaign["sent"]),
                    ("В очереди", campaign["pending"]),
                    ("Ошибки", campaign["failed"]),
                ]
            )
            + stop_line,
        ]
    )
