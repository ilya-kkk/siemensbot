import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)

from app.alerts import send_critical_alert
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.repositories import AppRepository
from app.services.telegram_errors import classify_telegram_error

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
settings = get_settings()


def _error_payload(exc: Exception) -> tuple[int | None, str, dict]:
    if isinstance(exc, TelegramUnauthorizedError):
        return 401, str(exc), {}
    if isinstance(exc, TelegramForbiddenError):
        return 403, str(exc), {}
    if isinstance(exc, TelegramRetryAfter):
        return 429, str(exc), {"retry_after": exc.retry_after}
    if isinstance(exc, TelegramBadRequest):
        return 400, str(exc), {}
    return None, str(exc), {}


async def _process_once(bot: Bot) -> int:
    async with SessionLocal() as session:
        repo = AppRepository(session)
        recipients = await repo.claim_due_recipients(settings.worker_claim_limit)

    for recipient in recipients:
        async with SessionLocal() as session:
            repo = AppRepository(session)
            try:
                sent = await bot.send_message(recipient["chat_id"], recipient["followup_text"])
                dialogue_id = await repo.get_or_create_dialogue(recipient["telegram_user_id"])
                await repo.log_message(
                    recipient["telegram_user_id"],
                    dialogue_id,
                    "outgoing",
                    recipient["followup_text"],
                    sent.message_id,
                    sent.model_dump(mode="json"),
                )
                await repo.mark_recipient_sent(recipient["recipient_id"], sent.message_id)
            except TelegramAPIError as exc:
                status_code, description, params = _error_payload(exc)
                action = classify_telegram_error(status_code or 500, description, params)
                await repo.mark_recipient_error(
                    recipient["recipient_id"],
                    recipient["telegram_user_id"],
                    action.recipient_status,
                    action.user_status,
                    status_code,
                    description,
                    action.retry_after_seconds,
                )
                if action.is_critical:
                    await send_critical_alert(session, settings, "telegram_delivery", description, {"recipient": recipient})
                    await repo.pause_running_campaigns()
            except Exception as exc:
                logger.exception("unexpected worker error")
                await repo.mark_recipient_error(
                    recipient["recipient_id"],
                    recipient["telegram_user_id"],
                    "failed",
                    None,
                    None,
                    str(exc),
                    None,
                )
    return len(recipients)


async def main() -> None:
    if not settings.user_bot_token:
        raise RuntimeError("USER_BOT_TOKEN is required")
    bot = Bot(settings.user_bot_token)
    try:
        while True:
            try:
                processed = await _process_once(bot)
                if processed:
                    logger.info("processed %s campaign recipients", processed)
            except Exception as exc:
                logger.exception("campaign worker loop failed")
                try:
                    async with SessionLocal() as session:
                        await send_critical_alert(session, settings, "campaign_worker", str(exc), {})
                except Exception:
                    logger.exception("failed to alert about worker failure")
            await asyncio.sleep(settings.worker_poll_seconds)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
