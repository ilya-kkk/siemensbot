import asyncio
import io
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, User

from app.ai.openrouter import OpenRouterClient, OpenRouterError
from app.alerts import (
    record_dependency_failure,
    record_dependency_success,
    send_critical_alert,
)
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.logging import configure_logging
from app.monitoring import heartbeat_loop, stop_background_task
from app.repositories import AppRepository

router = Router()
settings = get_settings()
configure_logging(settings)
logger = logging.getLogger(__name__)
test_sessions: dict[int, list[str]] = {}
test_offer_sent: set[int] = set()
VOICE_TRANSCRIPTION_ERROR = (
    "Не удалось расшифровать голосовое сообщение. Попробуйте записать ещё раз."
)
OFFER_CALLBACK_DATA = "register_lead"
TEST_OFFER_CALLBACK_DATA = "test_register_lead"
LEAD_ALREADY_REGISTERED_TEXT = (
    "Вы уже записаны на тест-драйв, скоро с вами свяжется менеджер."
)


def _openrouter_status_code(exc: OpenRouterError) -> int | None:
    value = exc.response_payload.get("status_code")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _offer_markup(callback_data: str = OFFER_CALLBACK_DATA) -> InlineKeyboardMarkup:
    button = InlineKeyboardButton(text="Записаться", callback_data=callback_data)
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


async def _build_offer_markup(_repo: AppRepository, _telegram_user_id: int) -> InlineKeyboardMarkup:
    return _offer_markup()


async def _client_bot_stopped() -> bool:
    async with SessionLocal() as session:
        return await AppRepository(session).is_client_bot_stopped()


async def _check_user_growth_alert() -> None:
    try:
        async with SessionLocal() as session:
            await AppRepository(session).trigger_due_user_growth_alert()
    except Exception as exc:
        logger.exception("failed to check user growth alert")
        await send_critical_alert(None, settings, "user_growth_alert", str(exc), {})


def _is_test_admin(user: User | None) -> bool:
    return bool(user and settings.admin_role_for_username(user.username))


def _render_test_transcript(chat_id: int) -> str:
    return "\n".join(test_sessions.get(chat_id, []))


def _message_text(message: Message) -> str | None:
    return message.text or message.caption


async def _register_incoming_message(
    message: Message,
    event_stage: str,
    text_value: str | None = None,
) -> tuple[int, int]:
    incoming_text = text_value if text_value is not None else _message_text(message)
    async with SessionLocal() as session:
        repo = AppRepository(session)
        telegram_user_id = await repo.upsert_telegram_user(
            chat_id=message.chat.id,
            telegram_user_id=message.from_user.id if message.from_user else None,
            username=message.from_user.username if message.from_user else None,
            first_name=message.from_user.first_name if message.from_user else None,
            last_name=message.from_user.last_name if message.from_user else None,
            event_stage=event_stage,
            activity_at=message.date,
            activity_message_id=message.message_id,
        )
        source_message_id = await repo.log_message(
            telegram_user_id,
            "incoming",
            incoming_text,
            message.message_id,
            message.model_dump(mode="json"),
            message_type="text" if incoming_text is not None else "service",
        )
    return telegram_user_id, source_message_id


async def _lead_user_id_for_message(message: Message) -> int | None:
    async with SessionLocal() as session:
        return await AppRepository(session).get_lead_user_id_for_chat(message.chat.id)


async def _send_lead_already_registered(message: Message, telegram_user_id: int) -> None:
    sent = await message.answer(LEAD_ALREADY_REGISTERED_TEXT)
    async with SessionLocal() as session:
        await AppRepository(session).log_message(
            telegram_user_id,
            "outgoing",
            LEAD_ALREADY_REGISTERED_TEXT,
            sent.message_id,
            sent.model_dump(mode="json"),
        )


async def _reply_if_user_is_lead(
    message: Message,
    telegram_user_id: int,
    user_snapshot: dict | None = None,
) -> bool:
    if user_snapshot is None:
        async with SessionLocal() as session:
            user_snapshot = await AppRepository(session).get_user_snapshot(telegram_user_id)
    if user_snapshot.get("funnel_stage") != "lead":
        return False
    await _send_lead_already_registered(message, telegram_user_id)
    return True


async def _ensure_user_analysis(
    telegram_user_id: int,
    source_message_id: int,
    client: OpenRouterClient,
) -> bool:
    async with SessionLocal() as session:
        repo = AppRepository(session)
        if not await repo.user_needs_analysis(telegram_user_id):
            return True
        transcript = await repo.get_transcript_for_user(telegram_user_id)
        user_snapshot = await repo.get_user_snapshot(telegram_user_id)

    try:
        analysis = await client.analyze_dialogue(transcript)
    except OpenRouterError as exc:
        async with SessionLocal() as session:
            repo = AppRepository(session)
            await repo.save_ai_request(
                telegram_user_id,
                source_message_id,
                "analysis",
                settings.openrouter_model,
                "failed",
                exc.request_payload,
                exc.response_payload,
                user_snapshot,
                None,
                str(exc),
            )
        await record_dependency_failure(
            settings,
            "user_bot",
            "openrouter",
            str(exc),
            status_code=_openrouter_status_code(exc),
            details={"purpose": "dialogue_analysis"},
        )
        return False

    await record_dependency_success(settings, "user_bot", "openrouter")

    async with SessionLocal() as session:
        repo = AppRepository(session)
        ai_request_id = await repo.save_ai_request(
            telegram_user_id,
            source_message_id,
            "analysis",
            settings.openrouter_model,
            "success",
            analysis.request_payload,
            analysis.response_payload,
            user_snapshot,
            analysis.usage,
        )
        await repo.save_user_analysis(telegram_user_id, ai_request_id, analysis.output)
    return True


@router.message(CommandStart())
async def start(message: Message) -> None:
    test_sessions.pop(message.chat.id, None)
    test_offer_sent.discard(message.chat.id)
    if await _client_bot_stopped():
        return

    telegram_user_id, _source_message_id = await _register_incoming_message(message, "started")
    await _check_user_growth_alert()
    if await _client_bot_stopped():
        return
    if await _reply_if_user_is_lead(message, telegram_user_id):
        return

    sent = await message.answer(settings.welcome_text)
    async with SessionLocal() as session:
        await AppRepository(session).log_message(
            telegram_user_id,
            "outgoing",
            settings.welcome_text,
            sent.message_id,
            sent.model_dump(mode="json"),
        )


@router.message(Command("test"))
async def test_start(message: Message) -> None:
    if not _is_test_admin(message.from_user):
        await message.answer("Команда недоступна.")
        return

    test_sessions[message.chat.id] = [f"outgoing: {settings.welcome_text}"]
    test_offer_sent.discard(message.chat.id)
    await message.answer(settings.welcome_text)


@router.message(lambda message: message.chat.id in test_sessions, F.text)
async def test_text_message(message: Message) -> None:
    if not _is_test_admin(message.from_user):
        test_sessions.pop(message.chat.id, None)
        test_offer_sent.discard(message.chat.id)
        return

    await _handle_test_message(message, message.text or "")


async def _handle_test_message(message: Message, user_text: str) -> None:
    transcript = _render_test_transcript(message.chat.id)
    client = OpenRouterClient(settings)
    try:
        decision = await client.chat_reply(transcript, user_text)
    except OpenRouterError:
        await message.answer("Сейчас не могу ответить.")
        return

    test_sessions[message.chat.id].append(f"incoming: {user_text}")
    test_sessions[message.chat.id].append(f"outgoing: {decision.reply_text}")

    offer_already_sent = message.chat.id in test_offer_sent
    reply_markup = (
        _offer_markup(TEST_OFFER_CALLBACK_DATA)
        if (decision.should_send_offer or offer_already_sent)
        else None
    )
    if decision.should_send_offer:
        test_offer_sent.add(message.chat.id)
    await message.answer(decision.reply_text, reply_markup=reply_markup)


@router.callback_query(F.data == TEST_OFFER_CALLBACK_DATA)
async def register_test_lead(callback: CallbackQuery) -> None:
    """Acknowledge the test button without changing production lead data."""
    await callback.answer("Тестовая заявка принята")


@router.callback_query(F.data == OFFER_CALLBACK_DATA)
async def register_lead(callback: CallbackQuery) -> None:
    """Turn a button click into a lead and enrich it from the complete dialogue."""
    await callback.answer()
    if callback.message is None:
        return

    chat_id = callback.message.chat.id
    external_user_id = callback.from_user.id if callback.from_user else None
    async with SessionLocal() as session:
        repo = AppRepository(session)
        lead_user_id = await repo.record_lead_click(chat_id, external_user_id)
        if lead_user_id is None:
            return
        source_message_id = await repo.get_message_id_for_telegram_message(
            lead_user_id,
            callback.message.message_id,
        )

    await callback.message.answer(
        "Ваша заявка принята, скоро с вами свяжется менеджер."
    )
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        logger.info("could not remove clicked lead button", exc_info=True)

    if source_message_id is None:
        logger.warning("lead %s has no source message for analysis", lead_user_id)
        return
    await _ensure_user_analysis(
        lead_user_id,
        source_message_id,
        OpenRouterClient(settings),
    )


async def _download_voice_bytes(bot: Bot, message: Message) -> bytes:
    if message.voice is None:
        return b""
    buffer = io.BytesIO()
    await bot.download(message.voice.file_id, destination=buffer)
    return buffer.getvalue()


async def _transcribe_voice(bot: Bot, message: Message) -> str | None:
    try:
        audio_bytes = await _download_voice_bytes(bot, message)
    except TelegramAPIError:
        logger.exception("Telegram voice download failed")
        await message.answer(VOICE_TRANSCRIPTION_ERROR)
        return None

    if not audio_bytes:
        logger.warning("Telegram voice download returned no data")
        await message.answer(VOICE_TRANSCRIPTION_ERROR)
        return None

    try:
        transcription = await OpenRouterClient(settings).transcribe_ogg(audio_bytes)
    except OpenRouterError as exc:
        logger.exception("OpenRouter voice transcription failed")
        await record_dependency_failure(
            settings,
            "user_bot",
            "openrouter",
            str(exc),
            status_code=_openrouter_status_code(exc),
            details={"purpose": "transcription"},
        )
        await message.answer(VOICE_TRANSCRIPTION_ERROR)
        return None

    if not transcription:
        logger.info("OpenRouter voice transcription was empty")
        await message.answer(VOICE_TRANSCRIPTION_ERROR)
        return None
    await record_dependency_success(settings, "user_bot", "openrouter")
    return transcription


@router.message(F.voice)
async def voice_message(message: Message, bot: Bot) -> None:
    is_test_session = message.chat.id in test_sessions
    if is_test_session:
        if not _is_test_admin(message.from_user):
            test_sessions.pop(message.chat.id, None)
            test_offer_sent.discard(message.chat.id)
            return
    elif await _client_bot_stopped():
        return

    if not is_test_session:
        lead_user_id = await _lead_user_id_for_message(message)
        if lead_user_id is not None:
            registered_user_id, _source_message_id = await _register_incoming_message(
                message,
                "dialogue",
            )
            await _send_lead_already_registered(message, registered_user_id)
            return

    transcription = await _transcribe_voice(bot, message)
    if transcription is None:
        return
    if is_test_session:
        await _handle_test_message(message, transcription)
        return
    await _handle_dialogue_message(message, transcription)


@router.message(F.text)
async def text_message(message: Message) -> None:
    await _handle_dialogue_message(message, message.text or "")


async def _handle_dialogue_message(message: Message, user_text: str) -> None:
    if await _client_bot_stopped():
        return

    telegram_user_id, source_message_id = await _register_incoming_message(
        message,
        "dialogue",
        text_value=user_text,
    )
    async with SessionLocal() as session:
        repo = AppRepository(session)
        user_snapshot = await repo.get_user_snapshot(telegram_user_id)
        if user_snapshot.get("funnel_stage") == "lead":
            transcript = None
        else:
            transcript = await repo.get_transcript_for_user(
                telegram_user_id,
                exclude_message_id=source_message_id,
            )

    if await _reply_if_user_is_lead(message, telegram_user_id, user_snapshot):
        return

    client = OpenRouterClient(settings)
    try:
        decision = await client.chat_reply(transcript, user_text)
    except OpenRouterError as exc:
        async with SessionLocal() as session:
            repo = AppRepository(session)
            ai_request_id = await repo.save_ai_request(
                telegram_user_id,
                source_message_id,
                "chat",
                settings.openrouter_model,
                "failed",
                exc.request_payload,
                exc.response_payload,
                user_snapshot,
                None,
                str(exc),
            )
        await record_dependency_failure(
            settings,
            "user_bot",
            "openrouter",
            str(exc),
            status_code=_openrouter_status_code(exc),
            details={"purpose": "chat"},
        )
        if await _client_bot_stopped():
            return
        sent = await message.answer("Сейчас не могу ответить. Уже чиним.")
        async with SessionLocal() as session:
            await AppRepository(session).log_message(
                telegram_user_id,
                "outgoing",
                "Сейчас не могу ответить. Уже чиним.",
                sent.message_id,
                sent.model_dump(mode="json"),
                message_type="error",
                ai_request_id=ai_request_id,
            )
        return
    provider = "local" if decision.request_payload.get("type") == "local_guard" else "openrouter"
    if provider == "openrouter":
        await record_dependency_success(settings, "user_bot", "openrouter")
    model = "local_guard" if provider == "local" else settings.openrouter_model
    async with SessionLocal() as session:
        repo = AppRepository(session)
        ai_request_id = await repo.save_ai_request(
            telegram_user_id,
            source_message_id,
            "chat",
            model,
            "success",
            decision.request_payload,
            decision.response_payload,
            user_snapshot,
            decision.usage,
            provider=provider,
        )
    if await _client_bot_stopped():
        return

    reply_markup = None
    message_type = "text"
    should_mark_offer_sent = False
    async with SessionLocal() as session:
        repo = AppRepository(session)
        if await repo.is_client_bot_stopped():
            return
        offer_already_sent = await repo.user_has_offer(telegram_user_id)
        if decision.should_send_offer or offer_already_sent:
            reply_markup = await _build_offer_markup(repo, telegram_user_id)
            message_type = "button"
            should_mark_offer_sent = decision.should_send_offer and not offer_already_sent

    sent = await message.answer(decision.reply_text, reply_markup=reply_markup)
    async with SessionLocal() as session:
        repo = AppRepository(session)
        await repo.log_message(
            telegram_user_id,
            "outgoing",
            decision.reply_text,
            sent.message_id,
            sent.model_dump(mode="json"),
            message_type=message_type,
            ai_request_id=ai_request_id,
        )
        if should_mark_offer_sent:
            await repo.mark_offer_sent(telegram_user_id)

@router.message()
async def non_text_message(message: Message) -> None:
    """Treat every inbound update as activity even when the dialogue accepts text only."""
    if await _client_bot_stopped():
        return
    telegram_user_id, _source_message_id = await _register_incoming_message(
        message,
        "dialogue",
    )
    await _reply_if_user_is_lead(message, telegram_user_id)


@router.errors()
async def errors(event) -> None:
    logger.exception("user bot error: %s", event.exception)
    await send_critical_alert(None, settings, "user_bot", str(event.exception), {})


async def main() -> None:
    if not settings.user_bot_token:
        raise RuntimeError("USER_BOT_TOKEN is required")
    async with SessionLocal() as session:
        await AppRepository(session).get_app_config()
    bot = Bot(settings.user_bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    heartbeat_task = asyncio.create_task(heartbeat_loop("user_bot", settings))
    try:
        await dp.start_polling(bot)
    except TelegramAPIError as exc:
        async with SessionLocal() as session:
            await send_critical_alert(session, settings, "user_bot", str(exc), {})
        raise
    except Exception as exc:
        await send_critical_alert(None, settings, "user_bot", str(exc), {})
        raise
    finally:
        await stop_background_task(heartbeat_task)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
