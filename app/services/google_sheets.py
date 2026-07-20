import asyncio
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from openpyxl.utils import get_column_letter

from app.services.leads import LEADS_XLSX_COLUMNS, group_leads_by_sheet

GOOGLE_SHEET_HEADERS = [header for _key, header in LEADS_XLSX_COLUMNS]

_SHEET_URL_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_DATED_SHEET_TITLE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_LEGACY_ID_HEADER = "SIEMENSBOT_LEAD_ID"


class GoogleSheetsError(RuntimeError):
    pass


def _cell_value(value: Any) -> str | int | float:
    if value is None:
        return ""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int | float | str):
        return value
    return str(value)


def _has_values(values: Sequence[Sequence[str]]) -> bool:
    return any(cell.strip() for row in values for cell in row)


def _google_worksheet_title(title: str) -> str:
    if _DATED_SHEET_TITLE_RE.fullmatch(title):
        return f"Бот {title}"
    if title == "Без даты":
        return "Бот Без даты"
    return "Бот"


class GoogleSheetsLeadSink:
    def __init__(
        self,
        spreadsheet: str,
        service_account_file: Path,
        *,
        document: Any | None = None,
    ) -> None:
        self.spreadsheet = spreadsheet.strip()
        self.service_account_file = service_account_file
        self._document = document

    def _connect(self) -> Any:
        if self._document is not None:
            return self._document
        if not self.spreadsheet:
            raise GoogleSheetsError("GOOGLE_SHEET is empty")
        if not self.service_account_file.is_file():
            raise GoogleSheetsError(
                f"Google service-account file does not exist: {self.service_account_file}"
            )
        try:
            import gspread

            client = gspread.service_account(filename=str(self.service_account_file))
            url_match = _SHEET_URL_RE.search(self.spreadsheet)
            if url_match:
                document = client.open_by_key(url_match.group(1))
            else:
                try:
                    document = client.open(self.spreadsheet)
                except gspread.SpreadsheetNotFound:
                    available = client.list_spreadsheet_files()
                    if len(available) != 1:
                        raise
                    document = client.open_by_key(available[0]["id"])
            self._document = document
            return self._document
        except GoogleSheetsError:
            raise
        except Exception as exc:
            raise GoogleSheetsError(f"Cannot open Google Sheet: {exc}") from exc

    @staticmethod
    def _can_reuse(worksheet: Any) -> bool:
        values = worksheet.get_all_values()
        return not _has_values(values) or bool(
            values
            and values[0]
            and (values[0][0] == _LEGACY_ID_HEADER or values[0] == GOOGLE_SHEET_HEADERS)
        )

    @staticmethod
    def _write_sheet(worksheet: Any, rows: Sequence[Mapping[str, Any]]) -> None:
        matrix = [
            GOOGLE_SHEET_HEADERS,
            *[[_cell_value(row.get(key)) for key, _header in LEADS_XLSX_COLUMNS] for row in rows],
        ]
        last_column = get_column_letter(len(GOOGLE_SHEET_HEADERS))
        worksheet.clear()
        worksheet.resize(rows=max(2, len(matrix)), cols=len(GOOGLE_SHEET_HEADERS))
        worksheet.update(
            values=matrix,
            range_name="A1",
            value_input_option="RAW",
        )
        worksheet.freeze(rows=1)
        worksheet.format(
            f"A1:{last_column}{len(matrix)}",
            {"verticalAlignment": "TOP", "wrapStrategy": "WRAP"},
        )
        worksheet.format(
            f"{last_column}2:{last_column}{max(2, len(matrix))}",
            {"wrapStrategy": "CLIP"},
        )
        worksheet.format(
            f"A1:{last_column}1",
            {
                "backgroundColor": {"red": 0.2, "green": 0.45, "blue": 0.75},
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            },
        )
        worksheet.columns_auto_resize(0, len(GOOGLE_SHEET_HEADERS))

    def _sync_leads(self, leads: Sequence[Mapping[str, Any]]) -> None:
        document = self._connect()
        grouped = [
            (_google_worksheet_title(title), rows) for title, rows in group_leads_by_sheet(leads)
        ]
        desired_titles = [title for title, _rows in grouped]
        worksheets = document.worksheets()
        by_title = {worksheet.title: worksheet for worksheet in worksheets}
        reusable = [
            worksheet
            for worksheet in worksheets
            if worksheet.title not in desired_titles and self._can_reuse(worksheet)
        ]

        synced_worksheets = []
        for index, (title, rows) in enumerate(grouped):
            worksheet = by_title.get(title)
            if worksheet is None and reusable:
                worksheet = reusable.pop(0)
                worksheet.update_title(title)
            if worksheet is None:
                worksheet = document.add_worksheet(
                    title=title,
                    rows=max(2, len(rows) + 1),
                    cols=len(GOOGLE_SHEET_HEADERS),
                    index=index,
                )
            self._write_sheet(worksheet, rows)
            synced_worksheets.append(worksheet)

        synced_ids = {worksheet.id for worksheet in synced_worksheets}
        remaining = [worksheet for worksheet in worksheets if worksheet.id not in synced_ids]
        document.reorder_worksheets([*synced_worksheets, *remaining])

    async def sync_leads(self, leads: Sequence[Mapping[str, Any]]) -> None:
        await asyncio.to_thread(self._sync_leads, leads)
