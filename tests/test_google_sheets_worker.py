from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.workers import google_sheets_worker


class _SessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_args):
        return None


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        google_sheet="Leads",
        google_service_account_file=Path("unused.json"),
        google_sheets_sync_batch_size=500,
    )


@pytest.mark.asyncio
async def test_process_once_rebuilds_export_and_marks_pending_batch(monkeypatch) -> None:
    pending = [{"id": 1}, {"id": 2}]
    all_leads = [{"id": 1, "username": "one"}, {"id": 2, "username": "two"}]
    repository = SimpleNamespace(
        try_google_sheet_sync_lock=AsyncMock(return_value=True),
        get_unsynced_google_sheet_leads=AsyncMock(return_value=pending),
        get_leads_for_export=AsyncMock(return_value=all_leads),
        mark_google_sheet_leads_synced=AsyncMock(return_value=2),
    )
    sink = SimpleNamespace(sync_leads=AsyncMock())
    monkeypatch.setattr(google_sheets_worker, "SessionLocal", _SessionContext)
    monkeypatch.setattr(google_sheets_worker, "AppRepository", lambda _session: repository)

    count = await google_sheets_worker._process_once(sink=sink, current_settings=_settings())

    assert count == 2
    sink.sync_leads.assert_awaited_once_with(all_leads)
    repository.mark_google_sheet_leads_synced.assert_awaited_once_with([1, 2])


@pytest.mark.asyncio
async def test_process_once_skips_when_another_worker_holds_lock(monkeypatch) -> None:
    repository = SimpleNamespace(
        try_google_sheet_sync_lock=AsyncMock(return_value=False),
        get_unsynced_google_sheet_leads=AsyncMock(),
    )
    sink = SimpleNamespace(sync_leads=AsyncMock())
    monkeypatch.setattr(google_sheets_worker, "SessionLocal", _SessionContext)
    monkeypatch.setattr(google_sheets_worker, "AppRepository", lambda _session: repository)

    count = await google_sheets_worker._process_once(sink=sink, current_settings=_settings())

    assert count == 0
    repository.get_unsynced_google_sheet_leads.assert_not_awaited()
    sink.sync_leads.assert_not_awaited()
