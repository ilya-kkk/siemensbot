import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, User

from app.ai.openrouter import OpenRouterClient, OpenRouterError
from app.alerts import send_critical_alert
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.repositories import AppRepository

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()
settings = get_settings()
test_sessions: dict[int, list[str]] = {}
test_offer_sent: set[int] = set()


def _offer_markup(url: str) -> InlineKeyboardMarkup:
    button = InlineKeyboardButton(text="Записаться", url=url)
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


async def _build_offer_markup(repo: AppRepository, telegram_user_id: int) -> InlineKeyboardMarkup:
    config = await repo.get_app_config(settings.test_drive_url)
    offer_url = str(config["offer_url"] or settings.test_drive_url)
    if settings.public_base_url:
        token = await repo.get_or_create_offer_token(telegram_user_id)
        offer_url = f"{settings.public_base_url.rstrip('/')}/r/{token}"
    return _offer_markup(offer_url)


async def _client_bot_stopped() -> bool:
    async with SessionLocal() as session:
        return await AppRepository(session).is_client_bot_stopped()


def _is_test_admin(user: User | None) -> bool:
    return bool(user and settings.admin_role_for_username(user.username))


def _render_test_transcript(chat_id: int) -> str:
    return "\n".join(test_sessions.get(chat_id, []))


def _message_text(message: Message) -> str | None:
    return message.text or message.caption


async def _register_incoming_message(message: Message, event_stage: str) -> tuple[int, int]:
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
            _message_text(message),
            message.message_id,
            message.model_dump(mode="json"),
            message_type="text" if _message_text(message) is not None else "service",
        )
    return telegram_user_id, source_message_id


async def _ensure_user_analysis(
    telegram_user_id: int,
    source_message_id: int,
    client: OpenRouterClient,
) -> None:
    async with SessionLocal() as session:
        repo = AppRepository(session)
        if not await repo.user_needs_analysis(telegram_user_id):
            return
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
            await send_critical_alert(
                session,
                settings,
                "dialogue_analysis",
                str(exc),
                {
                    "telegram_user_id": telegram_user_id,
                    "source_message_id": source_message_id,
                },
            )
        return

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


@router.message(CommandStart())
async def start(message: Message) -> None:
    test_sessions.pop(message.chat.id, None)
    test_offer_sent.discard(message.chat.id)
    if await _client_bot_stopped():
        return

    telegram_user_id, _source_message_id = await _register_incoming_message(message, "started")
    if await _client_bot_stopped():
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

    transcript = _render_test_transcript(message.chat.id)
    client = OpenRouterClient(settings)
    try:
        decision = await client.chat_reply(transcript, message.text or "")
    except OpenRouterError:
        await message.answer("Сейчас не могу ответить.")
        return

    test_sessions[message.chat.id].append(f"incoming: {message.text or ''}")
    test_sessions[message.chat.id].append(f"outgoing: {decision.reply_text}")

    offer_already_sent = message.chat.id in test_offer_sent
    async with SessionLocal() as session:
        config = await AppRepository(session).get_app_config(settings.test_drive_url)
    reply_markup = _offer_markup(str(config["offer_url"] or settings.test_drive_url)) if (
        decision.should_send_offer or offer_already_sent
    ) else None
    if decision.should_send_offer:
        test_offer_sent.add(message.chat.id)
    await message.answer(decision.reply_text, reply_markup=reply_markup)


@router.message(F.text)
async def text_message(message: Message) -> None:
    if await _client_bot_stopped():
        return

    telegram_user_id, source_message_id = await _register_incoming_message(message, "dialogue")
    async with SessionLocal() as session:
        repo = AppRepository(session)
        transcript = await repo.get_transcript_for_user(
            telegram_user_id,
            exclude_message_id=source_message_id,
        )
        user_snapshot = await repo.get_user_snapshot(telegram_user_id)

    client = OpenRouterClient(settings)
    try:
        decision = await client.chat_reply(transcript, message.text or "")
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
            await send_critical_alert(
                session,
                settings,
                "openrouter",
                str(exc),
                {
                    "telegram_user_id": telegram_user_id,
                    "source_message_id": source_message_id,
                },
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

    if reply_markup is not None:
        await _ensure_user_analysis(telegram_user_id, source_message_id, client)


@router.message()
async def non_text_message(message: Message) -> None:
    """Treat every inbound update as activity even when the dialogue accepts text only."""
    if await _client_bot_stopped():
        return
    await _register_incoming_message(message, "dialogue")


@router.errors()
async def errors(event) -> None:
    logger.exception("user bot error: %s", event.exception)


async def main() -> None:
    if not settings.user_bot_token:
        raise RuntimeError("USER_BOT_TOKEN is required")
    if settings.app_env.lower() not in {"local", "test"} and not settings.public_base_url:
        raise RuntimeError("PUBLIC_BASE_URL is required outside local/test environments")
    async with SessionLocal() as session:
        await AppRepository(session).get_app_config(settings.test_drive_url)
    bot = Bot(settings.user_bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    except TelegramAPIError as exc:
        async with SessionLocal() as session:
            await send_critical_alert(session, settings, "user_bot", str(exc), {})
        raise


if __name__ == "__main__":
    asyncio.run(main())
