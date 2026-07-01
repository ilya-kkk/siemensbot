import logging
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.repositories import AppRepository

logger = logging.getLogger(__name__)


async def send_critical_alert(
    session: AsyncSession,
    settings: Settings,
    category: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    repo = AppRepository(session)
    alert_id = await repo.create_alert("critical", category, message, details)
    chat_id = await repo.get_tech_admin_chat_id()
    if not chat_id or not settings.admin_bot_token:
        return

    text = f"🚨 CRITICAL\n<b>{category}</b>\n{message}"
    bot = Bot(settings.admin_bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    try:
        await bot.send_message(chat_id, text)
        await repo.mark_alert_delivered(alert_id, chat_id)
    except Exception:
        logger.exception("failed to deliver critical alert")
    finally:
        await bot.session.close()
