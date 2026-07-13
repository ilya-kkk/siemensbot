import asyncio
import logging
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    InputRichMessage,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.repositories import AppRepository
from app.services.admin_views import (
    format_ping_delays,
    parse_offer_url,
    parse_ping_delays,
    ping_delays_from_config,
    render_start_html,
    render_stats_rich_html,
)
from app.services.leads import render_leads_csv
from app.services.transcript import render_dialogue_html, split_telegram_html

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()
settings = get_settings()


MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="CSV"), KeyboardButton(text="Статистика")],
        [KeyboardButton(text="Диалог"), KeyboardButton(text="Установить ссылку")],
        [KeyboardButton(text="Настроить пинги"), KeyboardButton(text="Стоп")],
        [KeyboardButton(text="Отмена")],
    ],
    resize_keyboard=True,
)


class AdminStates(StatesGroup):
    waiting_dialog_query = State()
    waiting_offer_url = State()
    waiting_ping_delays = State()


async def _admin_for_identity(username: str | None, chat_id: int) -> tuple[int, str] | None:
    role = settings.admin_role_for_username(username)
    if not role:
        return None
    async with SessionLocal() as session:
        admin_id = await AppRepository(session).ensure_admin_user(username or "", role, chat_id)
    return admin_id, role


async def _ensure_admin(message: Message) -> tuple[int, str] | None:
    username = message.from_user.username if message.from_user else None
    admin = await _admin_for_identity(username, message.chat.id)
    if not admin:
        await message.answer("Нет доступа. Укажи TECH_ADMIN_USERNAME/BUSINESS_ADMIN_USERNAME в .env.")
    return admin


@router.message(CommandStart())
async def start(message: Message) -> None:
    if not await _ensure_admin(message):
        return
    await message.answer(render_start_html(), reply_markup=MENU, parse_mode="HTML")


@router.message(Command("leads"))
@router.message(F.text == "CSV")
async def download_leads(message: Message) -> None:
    if not await _ensure_admin(message):
        return
    async with SessionLocal() as session:
        rows = await AppRepository(session).get_leads_for_export()

    csv_bytes = render_leads_csv(rows)
    filename = f"leads_{datetime.now(UTC).strftime('%Y-%m-%d_%H-%M-%S')}.csv"
    await message.answer_document(
        BufferedInputFile(csv_bytes, filename=filename),
        caption=f"Лидов: {len(rows)}",
        reply_markup=MENU,
    )


@router.message(Command("stats"))
@router.message(F.text == "Статистика")
async def stats(message: Message) -> None:
    if not await _ensure_admin(message):
        return
    async with SessionLocal() as session:
        data = await AppRepository(session).get_stats()
    await message.answer_rich(
        InputRichMessage(html=render_stats_rich_html(data)),
        reply_markup=MENU,
    )


@router.message(Command("cancel"))
@router.message(F.text == "Отмена")
async def cancel(message: Message, state: FSMContext) -> None:
    if not await _ensure_admin(message):
        return
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=MENU)


@router.message(Command("offer_url"))
@router.message(F.text == "Установить ссылку")
async def offer_url_start(message: Message, state: FSMContext) -> None:
    if not await _ensure_admin(message):
        return
    async with SessionLocal() as session:
        config = await AppRepository(session).get_app_config(settings.test_drive_url)
    await state.set_state(AdminStates.waiting_offer_url)
    await message.answer(
        "Текущая ссылка:\n"
        f"{config['offer_url']}\n\n"
        "Пришли новый абсолютный адрес, начинающийся с http:// или https://.",
        reply_markup=MENU,
    )


@router.message(AdminStates.waiting_offer_url)
async def offer_url_receive(message: Message, state: FSMContext) -> None:
    if not await _ensure_admin(message):
        return
    try:
        url = parse_offer_url(message.text)
    except ValueError:
        await message.answer(
            "Не удалось распознать ссылку. Пришли абсолютный URL с http:// или https:// "
            "либо нажми «Отмена».",
            reply_markup=MENU,
        )
        return

    async with SessionLocal() as session:
        await AppRepository(session).set_offer_url(url)
    await state.clear()
    await message.answer(f"Ссылка установлена:\n{url}", reply_markup=MENU)


@router.message(Command("ping_settings"))
@router.message(F.text == "Настроить пинги")
async def ping_settings_start(message: Message, state: FSMContext) -> None:
    if not await _ensure_admin(message):
        return
    async with SessionLocal() as session:
        config = await AppRepository(session).get_app_config(settings.test_drive_url)
    current = format_ping_delays(ping_delays_from_config(config))
    await state.set_state(AdminStates.waiting_ping_delays)
    await message.answer(
        f"Текущие интервалы: {current}.\n\n"
        "Пришли три возрастающих числа в часах одним сообщением. "
        "Можно использовать дробные значения. Например: 2 24 72.",
        reply_markup=MENU,
    )


@router.message(AdminStates.waiting_ping_delays)
async def ping_settings_receive(message: Message, state: FSMContext) -> None:
    if not await _ensure_admin(message):
        return
    try:
        delays = parse_ping_delays(message.text)
    except ValueError:
        await message.answer(
            "Нужны три положительных возрастающих интервала в часах, например: 2 24 72. "
            "Либо нажми «Отмена».",
            reply_markup=MENU,
        )
        return

    async with SessionLocal() as session:
        await AppRepository(session).set_ping_delays(delays)
    await state.clear()
    await message.answer(f"Интервалы пингов сохранены: {format_ping_delays(delays)}.", reply_markup=MENU)


@router.message(Command("stop"))
@router.message(F.text == "Стоп")
async def stop_client_bot(message: Message) -> None:
    if not await _ensure_admin(message):
        return
    async with SessionLocal() as session:
        await AppRepository(session).set_client_bot_stopped(True)
    await message.answer(
        "<b>Аварийная остановка включена.</b>\n"
        "Клиентский бот больше не принимает и не отправляет сообщения.",
        reply_markup=MENU,
        parse_mode="HTML",
    )


@router.message(Command("dialog"))
@router.message(F.text == "Диалог")
async def dialogue_start(message: Message, state: FSMContext) -> None:
    if not await _ensure_admin(message):
        return
    await state.set_state(AdminStates.waiting_dialog_query)
    await message.answer("Пришли username или chat_id.")


@router.message(AdminStates.waiting_dialog_query)
async def dialogue_receive(message: Message, state: FSMContext) -> None:
    if not await _ensure_admin(message):
        return
    async with SessionLocal() as session:
        rows = await AppRepository(session).get_user_messages(message.text or "")
    html = render_dialogue_html(rows)
    for chunk in split_telegram_html(html):
        await message.answer(chunk, parse_mode="HTML")
    await state.clear()


async def main() -> None:
    if not settings.admin_bot_token:
        raise RuntimeError("ADMIN_BOT_TOKEN is required")
    bot = Bot(settings.admin_bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
