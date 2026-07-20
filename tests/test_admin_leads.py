from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bots import admin_bot
from app.services.admin_views import render_start_html


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


@pytest.mark.asyncio
async def test_download_leads_sends_timestamped_xlsx(monkeypatch) -> None:
    rows = [{"username": "client"}]
    repository = SimpleNamespace(get_leads_for_export=AsyncMock(return_value=rows))
    message = SimpleNamespace(answer_document=AsyncMock())

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 17, 15, 40, 5, tzinfo=UTC)

    monkeypatch.setattr(admin_bot, "_ensure_admin", AsyncMock(return_value=(1, "business")))
    monkeypatch.setattr(admin_bot, "SessionLocal", _SessionContext)
    monkeypatch.setattr(admin_bot, "AppRepository", lambda _session: repository)
    monkeypatch.setattr(admin_bot, "render_leads_xlsx", lambda export_rows: b"xlsx-bytes")
    monkeypatch.setattr(admin_bot, "datetime", FixedDatetime)

    await admin_bot.download_leads(message)

    repository.get_leads_for_export.assert_awaited_once_with()
    document = message.answer_document.await_args.args[0]
    assert document.filename == "leads_2026-07-17_15-40-05.xlsx"
    assert document.data == b"xlsx-bytes"
    assert message.answer_document.await_args.kwargs == {
        "caption": "Лидов: 1",
        "reply_markup": admin_bot.MENU,
    }


def test_admin_menu_and_help_use_report_label() -> None:
    assert admin_bot.MENU.keyboard[0][0].text == "Отчёт"
    assert "Отчёт - скачать HTML со всеми диалогами." in render_start_html()
    assert "Таблица -" not in render_start_html()
    assert "CSV -" not in render_start_html()


def test_download_leads_keeps_command_and_legacy_text_handlers() -> None:
    registered_handlers = [
        handler
        for handler in admin_bot.router.message.handlers
        if handler.callback is admin_bot.download_leads
    ]

    assert len(registered_handlers) == 3
