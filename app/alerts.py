import asyncio
import hashlib
import json
import logging
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.monitoring import cache_tech_admin_chat_id, read_cached_tech_admin_chat_id
from app.repositories import AppRepository

logger = logging.getLogger(__name__)

_last_sent: dict[str, datetime] = {}
_dependency_failures: dict[str, deque[datetime]] = defaultdict(deque)
_dependency_open: set[str] = set()
_dependency_successes: dict[str, int] = defaultdict(int)
_state_lock = asyncio.Lock()


def _redact(value: str, settings: Settings) -> str:
    result = value
    secrets = (
        settings.database_url,
        settings.admin_bot_token,
        settings.user_bot_token,
        settings.openrouter_api_key,
    )
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[REDACTED]")
    return result[:700]


def _fingerprint(category: str, message: str) -> str:
    normalized = " ".join(message.lower().split())[:240]
    return hashlib.sha256(f"{category}:{normalized}".encode()).hexdigest()[:20]


async def _resolve_chat_id(session: AsyncSession | None, settings: Settings) -> int | None:
    if session is None:
        try:
            from app.core.db import SessionLocal

            async with asyncio.timeout(3):
                async with SessionLocal() as owned_session:
                    chat_id = await AppRepository(owned_session).get_tech_admin_chat_id(
                        settings.tech_admin_username_normalized
                    )
            if chat_id is not None:
                return chat_id
        except Exception:
            logger.warning("failed to resolve tech admin from database; using cache", exc_info=True)
        return read_cached_tech_admin_chat_id(settings.tech_admin_chat_cache_path)

    if session is not None:
        try:
            chat_id = await AppRepository(session).get_tech_admin_chat_id(
                settings.tech_admin_username_normalized
            )
            if chat_id is not None:
                try:
                    cache_tech_admin_chat_id(settings.tech_admin_chat_cache_path, chat_id)
                except OSError:
                    # Only admin_bot owns the writable mount. Other services can
                    # still deliver using the value they just read from Postgres.
                    logger.debug("tech admin cache is read-only in this process")
                return chat_id
        except Exception:
            logger.warning("failed to resolve tech admin from database; using cache", exc_info=True)
    return read_cached_tech_admin_chat_id(settings.tech_admin_chat_cache_path)


async def _persist_alert(
    session: AsyncSession | None,
    severity: str,
    category: str,
    message: str,
    details: dict[str, Any],
    chat_id: int | None,
) -> None:
    owned_session = None
    try:
        if session is None:
            from app.core.db import SessionLocal

            owned_session = SessionLocal()
            session = owned_session
        async with asyncio.timeout(3):
            repo = AppRepository(session)
            alert_id = await repo.create_alert(severity, category, message, details)
            if chat_id is not None:
                await repo.mark_alert_delivered(alert_id, chat_id)
    except Exception:
        logger.warning("failed to persist alert", exc_info=True)
    finally:
        if owned_session is not None:
            await owned_session.close()


async def send_alert(
    session: AsyncSession | None,
    settings: Settings,
    severity: str,
    category: str,
    message: str,
    details: dict[str, Any] | None = None,
    *,
    force: bool = False,
) -> bool:
    safe_message = _redact(str(message), settings)
    safe_details_text = _redact(
        json.dumps(details or {}, default=str, ensure_ascii=False), settings
    )
    safe_details = {"summary": safe_details_text}
    fingerprint = _fingerprint(category, safe_message)
    now = datetime.now(UTC)

    async with _state_lock:
        previous = _last_sent.get(fingerprint)
        if not force and previous is not None and now - previous < timedelta(hours=1):
            return False
        _last_sent[fingerprint] = now

    chat_id = await _resolve_chat_id(session, settings)
    delivered = False
    if chat_id is not None and settings.admin_bot_token:
        icon = {"recovered": "✅", "info": "ℹ️"}.get(severity, "🚨")
        body = (
            f"{icon} <b>{escape(severity.upper())}</b> · {escape(settings.app_env)}\n"
            f"<b>{escape(category)}</b>\n"
            f"{escape(safe_message)}\n"
            f"<code>{now.strftime('%Y-%m-%d %H:%M:%S')} UTC</code>"
        )
        bot = Bot(
            settings.admin_bot_token,
            default=DefaultBotProperties(parse_mode="HTML"),
        )
        try:
            await bot.send_message(chat_id, body)
            delivered = True
        except Exception:
            logger.exception("failed to deliver alert through admin bot")
        finally:
            await bot.session.close()
    else:
        logger.error("alert recipient is unavailable: %s: %s", category, safe_message)

    if not delivered:
        async with _state_lock:
            if _last_sent.get(fingerprint) == now:
                _last_sent.pop(fingerprint, None)

    await _persist_alert(
        session,
        "info" if severity == "recovered" else severity,
        category,
        safe_message,
        {**safe_details, "fingerprint": fingerprint, "delivered": delivered},
        chat_id if delivered else None,
    )
    return delivered


async def send_critical_alert(
    session: AsyncSession | None,
    settings: Settings,
    category: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    await send_alert(session, settings, "critical", category, message, details)


async def record_dependency_failure(
    settings: Settings,
    component: str,
    provider: str,
    message: str,
    *,
    status_code: int | None = None,
    details: dict[str, Any] | None = None,
) -> bool:
    key = f"{component}:{provider}"
    now = datetime.now(UTC)
    permanent = status_code is not None and 400 <= status_code < 500 and status_code != 429
    async with _state_lock:
        failures = _dependency_failures[key]
        failures.append(now)
        cutoff = now - timedelta(minutes=5)
        while failures and failures[0] < cutoff:
            failures.popleft()
        should_alert = permanent or len(failures) >= 3
        _dependency_successes[key] = 0
        if should_alert:
            _dependency_open.add(key)
    if not should_alert:
        return False
    return await send_alert(
        None,
        settings,
        "critical",
        f"{component}:{provider}",
        message,
        {**(details or {}), "status_code": status_code, "failures_in_5m": len(failures)},
    )


async def record_dependency_success(settings: Settings, component: str, provider: str) -> bool:
    key = f"{component}:{provider}"
    async with _state_lock:
        if key not in _dependency_open:
            return False
        _dependency_successes[key] += 1
        if _dependency_successes[key] < 3:
            return False
    delivered = await send_alert(
        None,
        settings,
        "recovered",
        f"{component}:{provider}",
        "Dependency recovered after three successful requests",
        force=True,
    )
    if delivered:
        async with _state_lock:
            _dependency_open.discard(key)
            _dependency_failures.pop(key, None)
            _dependency_successes.pop(key, None)
    return delivered
