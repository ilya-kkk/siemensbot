from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services.google_sheets import GOOGLE_SHEET_HEADERS, GoogleSheetsLeadSink
from app.services.leads import LEADS_XLSX_COLUMNS


class _Worksheet:
    _next_id = 1

    def __init__(self, title: str, values: list[list[str]] | None = None) -> None:
        self.id = self._next_id
        type(self)._next_id += 1
        self.title = title
        self.values = values or []
        self.resize_calls: list[dict] = []
        self.freeze_calls: list[dict] = []
        self.format_calls: list[tuple[str, dict]] = []
        self.auto_resize_calls: list[tuple[int, int]] = []

    def get_all_values(self) -> list[list[str]]:
        return self.values

    def update_title(self, title: str) -> None:
        self.title = title

    def clear(self) -> None:
        self.values = []

    def resize(self, **kwargs) -> None:
        self.resize_calls.append(kwargs)

    def update(self, *, values, **_kwargs) -> None:
        self.values = values

    def freeze(self, **kwargs) -> None:
        self.freeze_calls.append(kwargs)

    def format(self, cell_range: str, value: dict) -> None:
        self.format_calls.append((cell_range, value))

    def columns_auto_resize(self, start: int, end: int) -> None:
        self.auto_resize_calls.append((start, end))


class _Document:
    def __init__(self, worksheets: list[_Worksheet]) -> None:
        self.items = worksheets

    def worksheets(self) -> list[_Worksheet]:
        return list(self.items)

    def add_worksheet(self, *, title: str, rows: int, cols: int, index: int) -> _Worksheet:
        worksheet = _Worksheet(title)
        worksheet.resize_calls.append({"rows": rows, "cols": cols})
        self.items.insert(index, worksheet)
        return worksheet

    def reorder_worksheets(self, worksheets) -> None:
        self.items = list(worksheets)


def _lead(lead_id: int, created_at: datetime | None, username: str) -> dict:
    return {
        "id": lead_id,
        "created_at": created_at,
        "username": username,
        "name": f"Lead {lead_id}",
        "telegram_name": f"Telegram {lead_id}",
        "niche": "Consulting",
        "main_problem": "Sales",
        "average_check": "100",
        "revenue_estimate": "1000",
        "sales_volume": "10",
        "lead_temperature": "warm",
        "confidence": 0.9,
        "summary": "Summary",
    }


@pytest.mark.asyncio
async def test_sync_matches_admin_export_columns_and_daily_sheets() -> None:
    legacy = _Worksheet("Sheet1", [["SIEMENSBOT_LEAD_ID", "Дата лида"], ["99", "old"]])
    document = _Document([legacy])
    sink = GoogleSheetsLeadSink("Leads", Path("unused.json"), document=document)
    leads = [
        _lead(1, datetime(2026, 7, 20, 10, tzinfo=UTC), "older"),
        _lead(2, datetime(2026, 7, 20, 12, tzinfo=UTC), "newer"),
        _lead(3, datetime(2026, 7, 19, 21, 30, tzinfo=UTC), "moscow-next-day"),
    ]

    await sink.sync_leads(leads)

    assert GOOGLE_SHEET_HEADERS == [header for _key, header in LEADS_XLSX_COLUMNS]
    assert [worksheet.title for worksheet in document.items] == ["Бот 20.07.2026"]
    assert document.items[0] is legacy
    assert legacy.values[0] == GOOGLE_SHEET_HEADERS
    assert len(legacy.values[0]) == len(LEADS_XLSX_COLUMNS)
    assert [row[0] for row in legacy.values[1:]] == ["newer", "older", "moscow-next-day"]
    assert legacy.freeze_calls == [{"rows": 1}]
    assert ("K2:K4", {"wrapStrategy": "CLIP"}) in legacy.format_calls


@pytest.mark.asyncio
async def test_sync_creates_one_sheet_for_each_moscow_day_and_undated_rows() -> None:
    document = _Document([_Worksheet("Sheet1")])
    sink = GoogleSheetsLeadSink("Leads", Path("unused.json"), document=document)
    leads = [
        _lead(1, datetime(2026, 7, 20, 20, 59, tzinfo=UTC), "day-20"),
        _lead(2, datetime(2026, 7, 20, 21, 0, tzinfo=UTC), "day-21"),
        _lead(3, None, "undated"),
    ]

    await sink.sync_leads(leads)

    assert [worksheet.title for worksheet in document.items] == [
        "Бот 21.07.2026",
        "Бот 20.07.2026",
        "Бот Без даты",
    ]
    assert all(worksheet.values[0] == GOOGLE_SHEET_HEADERS for worksheet in document.items)


@pytest.mark.asyncio
async def test_empty_export_uses_leads_sheet_with_only_business_header() -> None:
    document = _Document([_Worksheet("Sheet1", [[]])])
    sink = GoogleSheetsLeadSink("Leads", Path("unused.json"), document=document)

    await sink.sync_leads([])

    assert [worksheet.title for worksheet in document.items] == ["Бот"]
    assert document.items[0].values == [GOOGLE_SHEET_HEADERS]
