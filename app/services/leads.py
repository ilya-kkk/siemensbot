import csv
import io
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

LEADS_CSV_COLUMNS: list[tuple[str, str]] = [
    ("username", "Юзернейм"),
    ("name", "Имя"),
    ("telegram_name", "Имя в Telegram"),
    ("niche", "Ниша"),
    ("main_problem", "Основная проблема"),
    ("average_check", "Средний чек"),
    ("revenue_estimate", "Заработок"),
    ("sales_volume", "Количество продаж"),
    ("lead_temperature", "Температура лида"),
    ("confidence", "Уверенность анализа"),
    ("summary", "Краткое резюме"),
]


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def render_leads_csv(rows: Sequence[Mapping[str, Any]]) -> bytes:
    buffer = io.StringIO(newline="")
    fieldnames = [header for _, header in LEADS_CSV_COLUMNS]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({header: _csv_value(row.get(key)) for key, header in LEADS_CSV_COLUMNS})
    return buffer.getvalue().encode("utf-8-sig")
