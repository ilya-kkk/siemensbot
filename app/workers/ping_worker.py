import asyncio
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.ai.openrouter import OpenRouterClient, OpenRouterError, parse_ping_response
from app.alerts import (
    record_dependency_failure,
    record_dependency_success,
    send_critical_alert,
)
from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.core.logging import configure_logging
from app.monitoring import heartbeat_loop, stop_background_task
from app.repositories import AppRepository
from app.services.telegram_errors import classify_telegram_error

settings = get_settings()
configure_logging(settings)
logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 600
DEFAULT_POLL_SECONDS = 15
DEFAULT_RETRY_SECONDS = 300


def _setting_int(current_settings: Settings, name: str, default: int) -> int:
    try:
        return max(1, int(getattr(current_settings, name, default)))
    except (TypeError, ValueError):
        return default


def _retry_at(seconds: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=max(1, seconds))


def _openrouter_status_code(exc: OpenRouterError) -> int | None:
    value = exc.response_payload.get("status_code")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _ping_delays(row: Mapping[str, Any]) -> tuple[int, int, int]:
    return (
        int(row["ping_1_delay_minutes"]),
        int(row["ping_2_delay_minutes"]),
        int(row["ping_3_delay_minutes"]),
    )


def _extract_pending_ping_text(payload: Any, transcript: str = "") -> str | None:
    """Extract the generated text from a persisted raw OpenRouter response."""

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, Mapping):
        return None

    direct_text = payload.get("text")
    if isinstance(direct_text, str) and direct_text.strip():
        try:
            return parse_ping_response(
                {"choices": [{"message": {"content": json.dumps({"text": direct_text})}}]},
                transcript,
            )
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    try:
        return parse_ping_response(payload, transcript)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _telegram_error_payload(exc: TelegramAPIError) -> tuple[int, str, dict[str, Any]]:
    if isinstance(exc, TelegramUnauthorizedError):
        return 401, str(exc), {}
    if isinstance(exc, TelegramForbiddenError):
        return 403, str(exc), {}
    if isinstance(exc, TelegramRetryAfter):
        return 429, str(exc), {"retry_after": exc.retry_after}
    if isinstance(exc, TelegramNotFound):
        return 404, str(exc), {}
    if isinstance(exc, TelegramBadRequest):
        return 400, str(exc), {}
    return 500, str(exc), {}


def _message_payload(message: Any) -> dict[str, Any]:
    model_dump = getattr(message, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump(mode="json"))
    return {}


async def _offer_markup(
    _repo: AppRepository,
    _current_settings: Settings,
    _telegram_user_id: int,
    claim: Mapping[str, Any],
) -> InlineKeyboardMarkup | None:
    if not claim.get("offer_shown"):
        return None

    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Записаться", callback_data="register_lead")
        ]]
    )


async def _release_claim(
    telegram_user_id: int,
    claim_token: Any,
    retry_at: datetime | None,
    *,
    preserve_pending: bool,
) -> None:
    async with SessionLocal() as session:
        await AppRepository(session).release_ping_claim(
            telegram_user_id,
            claim_token,
            retry_at,
            preserve_pending=preserve_pending,
        )


async def _generate_ping(
    client: OpenRouterClient,
    current_settings: Settings,
    claim: Mapping[str, Any],
) -> tuple[str, int] | None:
    telegram_user_id = int(claim["telegram_user_id"])
    claim_token = claim["claim_token"]
    ping_number = int(claim["ping_number"])
    delays = _ping_delays(claim)

    async with SessionLocal() as session:
        repo = AppRepository(session)
        if await repo.is_client_bot_stopped():
            await repo.release_ping_claim(
                telegram_user_id,
                claim_token,
                None,
                preserve_pending=False,
            )
            return None
        source_message_id = await repo.create_ping_trigger(
            telegram_user_id,
            ping_number,
            claim.get("anchor_at"),
            delays,
        )
        transcript = await repo.get_transcript_for_user(
            telegram_user_id,
            exclude_message_id=source_message_id,
        )
        user_snapshot = await repo.get_user_snapshot(telegram_user_id)

    try:
        result = await client.generate_ping(
            transcript,
            ping_number,
            max(0, int(claim.get("idle_minutes") or 0)),
        )
    except OpenRouterError as exc:
        logger.warning("ping generation failed for user %s: %s", telegram_user_id, exc)
        async with SessionLocal() as session:
            repo = AppRepository(session)
            await repo.save_ai_request(
                telegram_user_id,
                source_message_id,
                "ping",
                current_settings.openrouter_model,
                "failed",
                exc.request_payload,
                exc.response_payload,
                user_snapshot,
                None,
                str(exc),
            )
            await repo.release_ping_claim(
                telegram_user_id,
                claim_token,
                _retry_at(
                    _setting_int(
                        current_settings,
                        "ping_worker_retry_seconds",
                        DEFAULT_RETRY_SECONDS,
                    )
                ),
                preserve_pending=False,
            )
        await record_dependency_failure(
            current_settings,
            "ping_worker",
            "openrouter",
            str(exc),
            status_code=_openrouter_status_code(exc),
            details={"purpose": "ping"},
        )
        return None

    await record_dependency_success(current_settings, "ping_worker", "openrouter")

    async with SessionLocal() as session:
        repo = AppRepository(session)
        ai_request_id = await repo.save_ai_request(
            telegram_user_id,
            source_message_id,
            "ping",
            current_settings.openrouter_model,
            "success",
            result.request_payload,
            result.response_payload,
            user_snapshot,
            result.usage,
            commit=False,
        )
        attached = await repo.set_pending_ping_ai_request(
            telegram_user_id,
            claim_token,
            ai_request_id,
        )
    if not attached:
        logger.info("ping claim for user %s changed during generation", telegram_user_id)
        return None
    return result.text, ai_request_id


async def _handle_delivery_error(
    session: Any,
    repo: AppRepository,
    current_settings: Settings,
    claim: Mapping[str, Any],
    exc: TelegramAPIError,
) -> None:
    telegram_user_id = int(claim["telegram_user_id"])
    claim_token = claim["claim_token"]
    status_code, description, parameters = _telegram_error_payload(exc)
    action = classify_telegram_error(status_code, description, parameters)
    retry_seconds = action.retry_after_seconds or _setting_int(
        current_settings,
        "ping_worker_retry_seconds",
        DEFAULT_RETRY_SECONDS,
    )
    retry_at = None if action.user_status else _retry_at(retry_seconds)
    preserve_pending = action.user_status is None and (
        status_code in {400, 401, 429} or status_code >= 500
    )

    await repo.fail_ping_delivery(
        telegram_user_id,
        claim_token,
        user_status=action.user_status,
        retry_at=retry_at,
        preserve_pending=preserve_pending,
    )
    if action.user_status is None:
        await record_dependency_failure(
            current_settings,
            "ping_worker",
            "telegram",
            description,
            status_code=status_code,
            details={"telegram_user_id": telegram_user_id},
        )


async def _process_claim(
    bot: Bot,
    client: OpenRouterClient,
    current_settings: Settings,
    claim: Mapping[str, Any],
) -> bool:
    telegram_user_id = int(claim["telegram_user_id"])
    claim_token = claim["claim_token"]
    ping_number = int(claim["ping_number"])
    pending_ai_request_id = claim.get("ping_pending_ai_request_id")

    try:
        if pending_ai_request_id is not None:
            async with SessionLocal() as session:
                transcript = await AppRepository(session).get_transcript_for_user(telegram_user_id)
            text_value = _extract_pending_ping_text(
                claim.get("pending_response_payload"),
                transcript,
            )
            if text_value is None:
                logger.warning("pending ping %s has no reusable text", pending_ai_request_id)
                await _release_claim(
                    telegram_user_id,
                    claim_token,
                    _retry_at(
                        _setting_int(
                            current_settings,
                            "ping_worker_retry_seconds",
                            DEFAULT_RETRY_SECONDS,
                        )
                    ),
                    preserve_pending=False,
                )
                return False
            ai_request_id = int(pending_ai_request_id)
        else:
            generated = await _generate_ping(client, current_settings, claim)
            if generated is None:
                return False
            text_value, ai_request_id = generated
            pending_ai_request_id = ai_request_id

        invalid_claim = False
        async with SessionLocal() as session:
            repo = AppRepository(session)
            if await repo.is_client_bot_stopped():
                await repo.release_ping_claim(
                    telegram_user_id,
                    claim_token,
                    None,
                    preserve_pending=True,
                )
                return False
            # validate_ping_claim holds the user row through delivery. That gives an
            # inbound update or click a strict before/after order relative to the
            # send; complete_ping_send stores Telegram's own Message.date so an
            # inbound update that was already in flight is not misclassified.
            validated = await repo.validate_ping_claim(telegram_user_id, claim_token)
            if validated is None or int(validated["ping_number"]) != ping_number:
                invalid_claim = True
            else:
                try:
                    reply_markup = await _offer_markup(
                        repo,
                        current_settings,
                        telegram_user_id,
                        validated,
                    )
                    sent = await bot.send_message(
                        int(validated["chat_id"]),
                        text_value,
                        reply_markup=reply_markup,
                    )
                    await record_dependency_success(
                        current_settings,
                        "ping_worker",
                        "telegram",
                    )
                except TelegramAPIError as exc:
                    await _handle_delivery_error(
                        session,
                        repo,
                        current_settings,
                        claim,
                        exc,
                    )
                    return False

                completed = await repo.complete_ping_send(
                    telegram_user_id,
                    claim_token,
                    ping_number,
                    ai_request_id,
                    text_value,
                    getattr(sent, "message_id", None),
                    {
                        "event": "ping",
                        "ping_number": ping_number,
                        "telegram": _message_payload(sent),
                    },
                    message_type="button" if reply_markup is not None else "text",
                    sent_at=getattr(sent, "date", None),
                )
                if not completed:
                    logger.warning(
                        "ping was delivered but claim completion failed for user %s",
                        telegram_user_id,
                    )
                return completed

        if invalid_claim:
            await _release_claim(
                telegram_user_id,
                claim_token,
                None,
                preserve_pending=True,
            )
            return False

        # Kept for type-checkers: every validated branch returns above.
        return False
    except Exception:
        logger.exception("unexpected error while processing ping for user %s", telegram_user_id)
        await _release_claim(
            telegram_user_id,
            claim_token,
            _retry_at(
                _setting_int(
                    current_settings,
                    "ping_worker_retry_seconds",
                    DEFAULT_RETRY_SECONDS,
                )
            ),
            preserve_pending=pending_ai_request_id is not None,
        )
        return False


async def _process_once(
    bot: Bot,
    *,
    client: OpenRouterClient | None = None,
    current_settings: Settings | None = None,
) -> int:
    current_settings = current_settings or settings
    async with SessionLocal() as session:
        repo = AppRepository(session)
        if await repo.is_client_bot_stopped():
            return 0
        await repo.get_app_config()
        claims = await repo.claim_due_ping_users(
            1,
            _setting_int(current_settings, "ping_worker_lease_seconds", DEFAULT_LEASE_SECONDS),
        )

    if not claims:
        return 0
    client = client or OpenRouterClient(current_settings)
    sent_count = 0
    for claim in claims:
        if await _process_claim(bot, client, current_settings, claim):
            sent_count += 1
    return sent_count


async def main() -> None:
    if not settings.user_bot_token:
        raise RuntimeError("USER_BOT_TOKEN is required")
    bot = Bot(settings.user_bot_token)
    heartbeat_task = asyncio.create_task(heartbeat_loop("ping_worker", settings))
    try:
        while True:
            try:
                processed = await _process_once(bot)
                if processed:
                    logger.info("sent %s ping messages", processed)
            except Exception as exc:
                logger.exception("ping worker loop failed")
                try:
                    async with SessionLocal() as session:
                        await send_critical_alert(session, settings, "ping_worker", str(exc), {})
                except Exception:
                    logger.exception("failed to alert about ping worker failure")
            await asyncio.sleep(
                _setting_int(settings, "ping_worker_poll_seconds", DEFAULT_POLL_SECONDS)
            )
    finally:
        await stop_background_task(heartbeat_task)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
