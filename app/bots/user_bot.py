import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.ai.openrouter import OpenRouterClient, OpenRouterError
from app.alerts import send_critical_alert
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.repositories import AppRepository

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()
settings = get_settings()


def _offer_markup(url: str | None) -> InlineKeyboardMarkup:
    if url:
        button = InlineKeyboardButton(text="Тест-драйв", url=url)
    else:
        button = InlineKeyboardButton(text="Тест-драйв", callback_data="offer_intent")
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


async def _register_user(message: Message) -> tuple[int, int]:
    user = message.from_user
    async with SessionLocal() as session:
        repo = AppRepository(session)
        telegram_user_id = await repo.upsert_telegram_user(
            chat_id=message.chat.id,
            telegram_user_id=user.id if user else None,
            username=user.username if user else None,
            first_name=user.first_name if user else None,
            last_name=user.last_name if user else None,
            source="new_start",
        )
        dialogue_id = await repo.get_or_create_dialogue(telegram_user_id)
    return telegram_user_id, dialogue_id


@router.message(CommandStart())
async def start(message: Message) -> None:
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
    await message.answer("Привет. Напиши, что сейчас хочешь разобрать по росту проекта.")


@router.callback_query(F.data == "offer_intent")
async def offer_intent(callback: CallbackQuery) -> None:
    if callback.message:
        await callback.message.answer(settings.test_drive_url)
    await callback.answer("Ссылка отправлена.")


@router.message(F.text)
async def text_message(message: Message) -> None:
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
    sent = await message.answer(decision.reply_text)
    async with SessionLocal() as session:
        await AppRepository(session).log_message(
            telegram_user_id,
            dialogue_id,
            "outgoing",
            decision.reply_text,
            sent.message_id,
            sent.model_dump(mode="json"),
        )

    if decision.should_send_offer:
        async with SessionLocal() as session:
            repo = AppRepository(session)
            url = None
            if settings.public_base_url:
                token = await repo.create_tracked_link(telegram_user_id, dialogue_id, settings.test_drive_url)
                url = f"{settings.public_base_url.rstrip('/')}/r/{token}"
            offer = await message.answer("Вот ссылка на тест-драйв:", reply_markup=_offer_markup(url))
            await repo.log_message(
                telegram_user_id,
                dialogue_id,
                "outgoing",
                "Вот ссылка на тест-драйв:",
                offer.message_id,
                offer.model_dump(mode="json"),
                message_type="button",
            )
            await repo.mark_offer_sent(dialogue_id)
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


@router.errors()
async def errors(event) -> None:
    logger.exception("user bot error: %s", event.exception)


async def main() -> None:
    if not settings.user_bot_token:
        raise RuntimeError("USER_BOT_TOKEN is required")
    bot = Bot(settings.user_bot_token)
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    except TelegramAPIError as exc:
        async with SessionLocal() as session:
            await send_critical_alert(session, settings, "user_bot", str(exc), {})
        raise


if __name__ == "__main__":
    asyncio.run(main())
