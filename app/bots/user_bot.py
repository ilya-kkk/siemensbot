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


async def _build_offer_markup(repo: AppRepository, telegram_user_id: int, dialogue_id: int) -> InlineKeyboardMarkup:
    offer_url = settings.test_drive_url
    if settings.public_base_url:
        token = await repo.create_tracked_link(telegram_user_id, dialogue_id, settings.test_drive_url)
        offer_url = f"{settings.public_base_url.rstrip('/')}/r/{token}"
    return _offer_markup(offer_url)


async def _client_bot_stopped() -> bool:
    async with SessionLocal() as session:
        return await AppRepository(session).is_client_bot_stopped()


def _is_test_admin(user: User | None) -> bool:
    return bool(user and settings.admin_role_for_username(user.username))


def _render_test_transcript(chat_id: int) -> str:
    return "\n".join(test_sessions.get(chat_id, []))


async def _upsert_user(chat_id: int, user: User | None) -> int:
    async with SessionLocal() as session:
        telegram_user_id = await AppRepository(session).upsert_telegram_user(
            chat_id=chat_id,
            telegram_user_id=user.id if user else None,
            username=user.username if user else None,
            first_name=user.first_name if user else None,
            last_name=user.last_name if user else None,
            source="new_start",
        )
        await session.commit()
        return telegram_user_id


async def _register_user(message: Message) -> tuple[int, int]:
    telegram_user_id = await _upsert_user(message.chat.id, message.from_user)
    async with SessionLocal() as session:
        repo = AppRepository(session)
        dialogue_id = await repo.get_or_create_dialogue(telegram_user_id)
    return telegram_user_id, dialogue_id


async def _ensure_dialogue_analysis(telegram_user_id: int, dialogue_id: int, client: OpenRouterClient) -> None:
    async with SessionLocal() as session:
        repo = AppRepository(session)
        if await repo.has_dialogue_analysis(dialogue_id):
            return
        transcript = await repo.get_transcript_for_analysis(dialogue_id)

    try:
        analysis = await client.analyze_dialogue(transcript)
    except OpenRouterError as exc:
        async with SessionLocal() as session:
            await send_critical_alert(session, settings, "dialogue_analysis", str(exc), {"dialogue_id": dialogue_id})
        return

    async with SessionLocal() as session:
        repo = AppRepository(session)
        ai_request_id = await repo.save_ai_request(
            telegram_user_id,
            dialogue_id,
            "analysis",
            settings.openrouter_model,
            "success",
            analysis.request_payload,
            analysis.response_payload,
            analysis.usage,
        )
        await repo.save_dialogue_analysis(telegram_user_id, dialogue_id, ai_request_id, analysis.output)


@router.message(CommandStart())
async def start(message: Message) -> None:
    test_sessions.pop(message.chat.id, None)
    test_offer_sent.discard(message.chat.id)
    if await _client_bot_stopped():
        return
    telegram_user_id, dialogue_id = await _register_user(message)
    async with SessionLocal() as session:
        await AppRepository(session).log_message(
            telegram_user_id,
            dialogue_id,
            "incoming",
            message.text,
            message.message_id,
            message.model_dump(mode="json"),
        )
    if await _client_bot_stopped():
        return
    await message.answer("Привет. После бесплатного обучения лучше не гадать, а приложить его к твоей ситуации. В какой нише сейчас проект?")


@router.message(Command("test"))
async def test_start(message: Message) -> None:
    if not _is_test_admin(message.from_user):
        await message.answer("Команда недоступна.")
        return

    test_sessions[message.chat.id] = [f"outgoing: {settings.followup_text}"]
    test_offer_sent.discard(message.chat.id)
    await message.answer(settings.followup_text)


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
    reply_markup = (
        _offer_markup(settings.test_drive_url) if decision.should_send_offer or offer_already_sent else None
    )
    if decision.should_send_offer:
        test_offer_sent.add(message.chat.id)
    await message.answer(decision.reply_text, reply_markup=reply_markup)


@router.message(F.text)
async def text_message(message: Message) -> None:
    if await _client_bot_stopped():
        return
    telegram_user_id, dialogue_id = await _register_user(message)
    async with SessionLocal() as session:
        repo = AppRepository(session)
        await repo.log_message(
            telegram_user_id,
            dialogue_id,
            "incoming",
            message.text,
            message.message_id,
            message.model_dump(mode="json"),
        )
        transcript = await repo.get_transcript_for_analysis(dialogue_id)

    client = OpenRouterClient(settings)
    try:
        decision = await client.chat_reply(transcript, message.text or "")
    except OpenRouterError as exc:
        async with SessionLocal() as session:
            repo = AppRepository(session)
            await repo.save_ai_request(
                telegram_user_id,
                dialogue_id,
                "chat",
                settings.openrouter_model,
                "failed",
                {},
                {},
                None,
                str(exc),
            )
            await send_critical_alert(session, settings, "openrouter", str(exc), {"dialogue_id": dialogue_id})
        if await _client_bot_stopped():
            return
        await message.answer("Сейчас не могу ответить. Уже чиним.")
        return

    async with SessionLocal() as session:
        repo = AppRepository(session)
        await repo.save_ai_request(
            telegram_user_id,
            dialogue_id,
            "chat",
            settings.openrouter_model,
            "success",
            decision.request_payload,
            decision.response_payload,
            decision.usage,
        )
    if await _client_bot_stopped():
        return

    reply_markup = None
    message_type = "text"
    should_mark_offer_sent = False
    if decision.should_send_offer:
        should_mark_offer_sent = True
    async with SessionLocal() as session:
        repo = AppRepository(session)
        if await repo.is_client_bot_stopped():
            return
        offer_already_sent = await repo.dialogue_has_offer(dialogue_id)
        if decision.should_send_offer or offer_already_sent:
            reply_markup = await _build_offer_markup(repo, telegram_user_id, dialogue_id)
            message_type = "button"
            should_mark_offer_sent = decision.should_send_offer and not offer_already_sent

    sent = await message.answer(decision.reply_text, reply_markup=reply_markup)
    async with SessionLocal() as session:
        repo = AppRepository(session)
        await repo.log_message(
            telegram_user_id,
            dialogue_id,
            "outgoing",
            decision.reply_text,
            sent.message_id,
            sent.model_dump(mode="json"),
            message_type=message_type,
        )
        if should_mark_offer_sent:
            await repo.mark_offer_sent(dialogue_id)

    if decision.should_send_offer:
        await _ensure_dialogue_analysis(telegram_user_id, dialogue_id, client)


@router.errors()
async def errors(event) -> None:
    logger.exception("user bot error: %s", event.exception)


async def main() -> None:
    if not settings.user_bot_token:
        raise RuntimeError("USER_BOT_TOKEN is required")
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
