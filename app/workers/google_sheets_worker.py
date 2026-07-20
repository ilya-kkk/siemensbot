import asyncio
import logging

from app.alerts import record_dependency_failure, record_dependency_success
from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.core.logging import configure_logging
from app.monitoring import heartbeat_loop, stop_background_task
from app.repositories import AppRepository
from app.services.google_sheets import GoogleSheetsLeadSink

settings = get_settings()
configure_logging(settings)
logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 600
DEFAULT_BATCH_SIZE = 500


def _positive_setting(current_settings: Settings, name: str, default: int) -> int:
    try:
        return max(1, int(getattr(current_settings, name, default)))
    except (TypeError, ValueError):
        return default


def _build_sink(current_settings: Settings) -> GoogleSheetsLeadSink:
    spreadsheet = (current_settings.google_sheet or "").strip()
    if not spreadsheet:
        raise RuntimeError("GOOGLE_SHEET is required")
    return GoogleSheetsLeadSink(
        spreadsheet,
        current_settings.google_service_account_file,
    )


async def _process_once(
    *,
    sink: GoogleSheetsLeadSink | None = None,
    current_settings: Settings | None = None,
) -> int:
    current_settings = current_settings or settings
    sink = sink or _build_sink(current_settings)

    async with SessionLocal() as session:
        repository = AppRepository(session)
        if not await repository.try_google_sheet_sync_lock():
            logger.info("another Google Sheets sync is already running")
            return 0

        leads = await repository.get_unsynced_google_sheet_leads(
            _positive_setting(
                current_settings,
                "google_sheets_sync_batch_size",
                DEFAULT_BATCH_SIZE,
            )
        )
        if not leads:
            return 0

        all_leads = await repository.get_leads_for_export()
        await sink.sync_leads(all_leads)
        await repository.mark_google_sheet_leads_synced([int(lead["id"]) for lead in leads])
        return len(leads)


async def main() -> None:
    sink = _build_sink(settings)
    heartbeat_task = asyncio.create_task(heartbeat_loop("google_sheets_worker", settings))
    try:
        while True:
            try:
                synced = await _process_once(sink=sink)
                await record_dependency_success(settings, "google_sheets_worker", "google_sheets")
                if synced:
                    logger.info("synced %s leads to Google Sheets", synced)
            except Exception as exc:
                logger.exception("Google Sheets worker loop failed")
                await record_dependency_failure(
                    settings,
                    "google_sheets_worker",
                    "google_sheets",
                    str(exc),
                )
            await asyncio.sleep(
                _positive_setting(
                    settings,
                    "google_sheets_sync_interval_seconds",
                    DEFAULT_INTERVAL_SECONDS,
                )
            )
    finally:
        await stop_background_task(heartbeat_task)


if __name__ == "__main__":
    asyncio.run(main())
