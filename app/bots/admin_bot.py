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
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.repositories import AppRepository
from app.services.admin_views import (
    parse_campaign_settings,
    render_campaign_html,
    render_start_html,
    render_stats_html,
)
from app.services.importer import parse_import_text
from app.services.leads import render_leads_csv
from app.services.transcript import render_dialogue_html, split_telegram_html

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()
settings = get_settings()


MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Скачать таблицу"), KeyboardButton(text="Статистика")],
        [KeyboardButton(text="Кампания"), KeyboardButton(text="Диалог")],
        [KeyboardButton(text="Стоп")],
    ],
    resize_keyboard=True,
)


def _campaign_markup(campaign: dict | None) -> InlineKeyboardMarkup:
    status = campaign["status"] if campaign else None
    control = (
        InlineKeyboardButton(text="Возобновить", callback_data="campaign_resume")
        if status == "paused"
        else InlineKeyboardButton(text="Приостановить", callback_data="campaign_pause")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Настройка", callback_data="campaign_settings"),
                control,
            ]
        ]
    )


class AdminStates(StatesGroup):
    waiting_import = State()
    waiting_campaign_settings = State()
    waiting_dialog_query = State()


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


async def _ensure_admin_callback(callback: CallbackQuery) -> tuple[int, str] | None:
    username = callback.from_user.username if callback.from_user else None
    chat_id = callback.message.chat.id if callback.message else callback.from_user.id
    admin = await _admin_for_identity(username, chat_id)
    if not admin:
        await callback.answer("Нет доступа.", show_alert=True)
    return admin


async def _send_campaign_status(message: Message) -> None:
    async with SessionLocal() as session:
        repo = AppRepository(session)
        campaign = await repo.get_current_campaign()
        stopped = await repo.is_client_bot_stopped()
    await message.answer(
        render_campaign_html(campaign, stopped),
        reply_markup=_campaign_markup(campaign),
        parse_mode="HTML",
    )


@router.message(CommandStart())
async def start(message: Message) -> None:
    if not await _ensure_admin(message):
        return
    await message.answer(render_start_html(), reply_markup=MENU, parse_mode="HTML")


@router.message(Command("leads"))
@router.message(F.text == "Скачать таблицу")
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
    await message.answer(render_stats_html(data), reply_markup=MENU, parse_mode="HTML")


@router.message(Command("import"))
@router.message(F.text == "Импорт")
async def import_start(message: Message, state: FSMContext) -> None:
    if not await _ensure_admin(message):
        return
    await state.set_state(AdminStates.waiting_import)
    await message.answer("Пришли .txt/.csv файлом или вставь текст. Лучше формат: chat_id, username.")


@router.message(AdminStates.waiting_import)
async def import_receive(message: Message, state: FSMContext, bot: Bot) -> None:
    if not await _ensure_admin(message):
        return
    content = message.text or ""
    if message.document:
        file = await bot.get_file(message.document.file_id)
        downloaded = await bot.download_file(file.file_path)
        content = downloaded.read().decode("utf-8")
    users = parse_import_text(content)
    async with SessionLocal() as session:
        result = await AppRepository(session).import_users(users)
    await state.clear()
    await message.answer(
        f"Импорт: {result['imported']} с chat_id, {result['unresolved']} username-only, всего распознано {result['total']}.",
        reply_markup=MENU,
    )


@router.message(Command("campaign"))
@router.message(F.text == "Кампания")
async def campaign_start(message: Message) -> None:
    if not await _ensure_admin(message):
        return
    await _send_campaign_status(message)


@router.callback_query(F.data == "campaign_settings")
async def campaign_settings(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _ensure_admin_callback(callback):
        return
    await state.set_state(AdminStates.waiting_campaign_settings)
    if isinstance(callback.message, Message):
        await callback.message.answer("Пришлите размер батча и интервал одним сообщением. Например: 100, 30")
    await callback.answer()


@router.message(AdminStates.waiting_campaign_settings)
async def campaign_settings_receive(message: Message, state: FSMContext) -> None:
    admin = await _ensure_admin(message)
    if not admin:
        return
    try:
        batch_size, interval_minutes = parse_campaign_settings(message.text)
    except ValueError:
        await message.answer("Формат: 100, 30")
        return

    async with SessionLocal() as session:
        _campaign_id, action = await AppRepository(session).configure_current_campaign(
            batch_size=batch_size,
            interval_minutes=interval_minutes,
            created_by_admin_id=admin[0],
            followup_text=settings.followup_text,
        )
    await state.clear()
    action_text = "создана" if action == "created" else "обновлена"
    await message.answer(
        f"Кампания {action_text}: {batch_size} юзеров раз в {interval_minutes} минут.",
        reply_markup=MENU,
    )
    await _send_campaign_status(message)


@router.callback_query(F.data == "campaign_pause")
async def campaign_pause(callback: CallbackQuery) -> None:
    if not await _ensure_admin_callback(callback):
        return
    async with SessionLocal() as session:
        campaign_id = await AppRepository(session).pause_current_campaign()
    if isinstance(callback.message, Message):
        text = "Кампания поставлена на паузу." if campaign_id else "Работающей кампании нет."
        await callback.message.answer(text, reply_markup=MENU)
        await _send_campaign_status(callback.message)
    await callback.answer()


@router.callback_query(F.data == "campaign_resume")
async def campaign_resume(callback: CallbackQuery) -> None:
    if not await _ensure_admin_callback(callback):
        return
    async with SessionLocal() as session:
        campaign_id = await AppRepository(session).resume_current_campaign()
    if isinstance(callback.message, Message):
        text = "Кампания возобновлена." if campaign_id else "Кампании на паузе нет."
        await callback.message.answer(text, reply_markup=MENU)
        await _send_campaign_status(callback.message)
    await callback.answer()


@router.message(Command("stop"))
@router.message(F.text == "Стоп")
async def stop_campaigns(message: Message) -> None:
    if not await _ensure_admin(message):
        return
    async with SessionLocal() as session:
        paused = await AppRepository(session).emergency_stop_client_bot()
    await message.answer(
        (
            "<b>Аварийная остановка включена.</b>\n"
            "Клиентский бот больше не будет отправлять сообщения пользователям.\n"
            + ("Активная кампания поставлена на паузу." if paused else "Активной кампании не было.")
        ),
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
        rows = await AppRepository(session).get_dialogue_messages(message.text or "")
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
