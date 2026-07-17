import asyncio
import logging
import os
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from app.core.config import Settings

logger = logging.getLogger(__name__)


def _read_cached_int(path: Path) -> int | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
        return int(value) if value else None
    except (OSError, ValueError):
        return None


def _cache_int(path: Path, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(f"{value}\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def read_cached_tech_admin_chat_id(path: Path) -> int | None:
    return _read_cached_int(path)


def cache_tech_admin_chat_id(path: Path, chat_id: int) -> None:
    """Atomically cache the recipient so database outages remain alertable."""
    _cache_int(path, chat_id)


def read_cached_tech_status_message_id(path: Path) -> int | None:
    return _read_cached_int(path)


def cache_tech_status_message_id(path: Path, message_id: int) -> None:
    _cache_int(path, message_id)


def read_cached_business_admin_chat_id(path: Path) -> int | None:
    return _read_cached_int(path)


def cache_business_admin_chat_id(path: Path, chat_id: int) -> None:
    _cache_int(path, chat_id)


def read_cached_business_status_message_id(path: Path) -> int | None:
    return _read_cached_int(path)


def cache_business_status_message_id(path: Path, message_id: int) -> None:
    _cache_int(path, message_id)


async def refresh_tech_admin_chat_cache(settings: Settings) -> int | None:
    from app.core.db import SessionLocal
    from app.repositories import AppRepository

    async with SessionLocal() as session:
        chat_id = await AppRepository(session).get_tech_admin_chat_id(
            settings.tech_admin_username_normalized
        )
    if chat_id is not None:
        cache_tech_admin_chat_id(settings.tech_admin_chat_cache_path, chat_id)
    return chat_id


async def heartbeat_once(
    component: str, *, status: str = "ok", details: dict | None = None
) -> None:
    from app.core.db import SessionLocal
    from app.repositories import AppRepository

    async with SessionLocal() as session:
        await AppRepository(session).upsert_service_heartbeat(component, status, details or {})


async def heartbeat_loop(component: str, settings: Settings) -> None:
    delay = max(5, settings.heartbeat_interval_seconds)
    while True:
        try:
            await heartbeat_once(component)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("heartbeat update failed for %s", component, exc_info=True)
        await asyncio.sleep(delay)


async def stop_background_task(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
