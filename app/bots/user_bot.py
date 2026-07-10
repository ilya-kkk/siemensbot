import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, User

from app.ai.openrouter import OpenRouterClient, OpenRouterError
from app.alerts import send_critical_alert
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.repositories import AppRepository

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()
settings = get_settings()


class LeadStates(StatesGroup):
    waiting_name = State()


def _offer_markup(dialogue_id: int) -> InlineKeyboardMarkup:
    button = InlineKeyboardButton(text="Записаться", callback_data=f"book_test_drive:{dialogue_id}")
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


def _dialogue_id_from_callback(data: str | None) -> int | None:
    if not data or not data.startswith("book_test_drive:"):
        return None
    value = data.removeprefix("book_test_drive:")
    return int(value) if value.isdigit() else None


async def _client_bot_stopped() -> bool:
    async with SessionLocal() as session:
        return await AppRepository(session).is_client_bot_stopped()


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
    await message.answer("Привет. Напиши, что сейчас хочешь разобрать по росту проекта.")


@router.callback_query(F.data == "offer_intent")
@router.callback_query(F.data.startswith("book_test_drive:"))
async def book_test_drive(callback: CallbackQuery, state: FSMContext) -> None:
    if await _client_bot_stopped():
        return
    if not isinstance(callback.message, Message):
        await callback.answer("Напишите в чат, и мы вас запишем.")
        return

    telegram_user_id = await _upsert_user(callback.message.chat.id, callback.from_user)
    dialogue_id = _dialogue_id_from_callback(callback.data)
    if dialogue_id is None:
        async with SessionLocal() as session:
            dialogue_id = await AppRepository(session).get_or_create_dialogue(telegram_user_id)

    async with SessionLocal() as session:
        await AppRepository(session).log_message(
            telegram_user_id,
            dialogue_id,
            "incoming",
            "Нажал кнопку «Записаться»",
            None,
            callback.model_dump(mode="json"),
            message_type="button",
        )

    if await _client_bot_stopped():
        return
    await state.update_data(telegram_user_id=telegram_user_id, dialogue_id=dialogue_id)
    await state.set_state(LeadStates.waiting_name)
    await callback.message.answer("Как к вам обращаться?")
    await callback.answer()


@router.message(LeadStates.waiting_name, F.text)
async def lead_name_received(message: Message, state: FSMContext) -> None:
    if await _client_bot_stopped():
        return
    contact_name = (message.text or "").strip()
    if not contact_name:
        await message.answer("Напишите, пожалуйста, имя текстом.")
        return

    data = await state.get_data()
    telegram_user_id = int(data["telegram_user_id"])
    dialogue_id = int(data["dialogue_id"])

    async with SessionLocal() as session:
        await AppRepository(session).log_message(
            telegram_user_id,
            dialogue_id,
            "incoming",
            contact_name,
            message.message_id,
            message.model_dump(mode="json"),
        )

    client = OpenRouterClient(settings)
    await _ensure_dialogue_analysis(telegram_user_id, dialogue_id, client)

    async with SessionLocal() as session:
        await AppRepository(session).create_lead(telegram_user_id, dialogue_id, contact_name)

    await state.clear()
    if await _client_bot_stopped():
        return
    await message.answer("Отлично, вы записаны, скоро с вами свяжется наша команда. Спасибо.")


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
            offer_text = "Можем записать вас на тест-драйв."
            if await repo.is_client_bot_stopped():
                return
            offer = await message.answer(offer_text, reply_markup=_offer_markup(dialogue_id))
            await repo.log_message(
                telegram_user_id,
                dialogue_id,
                "outgoing",
                offer_text,
                offer.message_id,
                offer.model_dump(mode="json"),
                message_type="button",
            )
            await repo.mark_offer_sent(dialogue_id)

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
