import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.repositories import AppRepository
from app.services.importer import parse_import_text
from app.services.transcript import render_dialogue_html, split_telegram_html

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()
settings = get_settings()


MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Импорт"), KeyboardButton(text="Кампания")],
        [KeyboardButton(text="Статистика"), KeyboardButton(text="Диалог")],
        [KeyboardButton(text="Стоп"), KeyboardButton(text="Реконфиг")],
    ],
    resize_keyboard=True,
)


class AdminStates(StatesGroup):
    waiting_import = State()
    waiting_campaign_text = State()
    waiting_batch_size = State()
    waiting_interval = State()
    waiting_dialog_query = State()
    waiting_reconfigure = State()


@dataclass
class CampaignDraft:
    text: str
    batch_size: int | None = None


drafts: dict[int, CampaignDraft] = {}


async def _ensure_admin(message: Message) -> tuple[int, str] | None:
    username = message.from_user.username if message.from_user else None
    role = settings.admin_role_for_username(username)
    if not role:
        await message.answer("Нет доступа. Укажи TECH_ADMIN_USERNAME/BUSINESS_ADMIN_USERNAME в .env.")
        return None
    async with SessionLocal() as session:
        repo = AppRepository(session)
        admin_id = await repo.ensure_admin_user(username or "", role, message.chat.id)
    return admin_id, role


@router.message(CommandStart())
async def start(message: Message) -> None:
    if not await _ensure_admin(message):
        return
    await message.answer("Админка готова.", reply_markup=MENU)


@router.message(Command("stats"))
@router.message(F.text == "Статистика")
async def stats(message: Message) -> None:
    if not await _ensure_admin(message):
        return
    async with SessionLocal() as session:
        data = await AppRepository(session).get_stats()
    await message.answer(
        "\n".join(
            [
                f"Всего юзеров: {data['total_users']}",
                f"Старых юзеров: {data['old_users']}",
                f"Follow-up отправлено: {data['sent']} / {data['total_recipients']} ({data['sent_percent']}%)",
                f"В очереди: {data['pending']}",
                f"Ошибки отправки: {data['failed']}",
                f"Blocked 403: {data['blocked_users']}",
                f"Invalid chat_id: {data['invalid_users']}",
                f"Unresolved username-only: {data['unresolved_users']}",
                f"Клики по кнопке: {data['button_clicks']}",
                f"AI cost USD: {data['ai_cost_usd']}",
            ]
        )
    )


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


@router.message(F.text == "Кампания")
async def campaign_start(message: Message, state: FSMContext) -> None:
    if not await _ensure_admin(message):
        return
    await state.set_state(AdminStates.waiting_campaign_text)
    await message.answer("Пришли текст follow-up сообщения.")


@router.message(AdminStates.waiting_campaign_text)
async def campaign_text(message: Message, state: FSMContext) -> None:
    if not await _ensure_admin(message):
        return
    drafts[message.chat.id] = CampaignDraft(text=message.text or "")
    await state.set_state(AdminStates.waiting_batch_size)
    await message.answer("Размер батча? Например: 100")


@router.message(AdminStates.waiting_batch_size)
async def campaign_batch(message: Message, state: FSMContext) -> None:
    if not await _ensure_admin(message):
        return
    if not message.text or not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("Нужно положительное число.")
        return
    drafts[message.chat.id].batch_size = int(message.text)
    await state.set_state(AdminStates.waiting_interval)
    await message.answer("Интервал между батчами в минутах? Например: 30")


@router.message(AdminStates.waiting_interval)
async def campaign_interval(message: Message, state: FSMContext) -> None:
    admin = await _ensure_admin(message)
    if not admin:
        return
    if not message.text or not message.text.isdigit() or int(message.text) <= 0:
        await message.answer("Нужно положительное число минут.")
        return
    draft = drafts.pop(message.chat.id)
    async with SessionLocal() as session:
        campaign_id = await AppRepository(session).create_campaign(
            name="follow-up",
            followup_text=draft.text,
            batch_size=draft.batch_size or 100,
            interval_minutes=int(message.text),
            created_by_admin_id=admin[0],
        )
    await state.clear()
    await message.answer(f"Кампания #{campaign_id} создана и запущена.", reply_markup=MENU)


@router.message(F.text == "Стоп")
async def stop_campaigns(message: Message) -> None:
    if not await _ensure_admin(message):
        return
    async with SessionLocal() as session:
        count = await AppRepository(session).pause_running_campaigns()
    await message.answer(f"Остановлено кампаний: {count}", reply_markup=MENU)


@router.message(F.text == "Реконфиг")
async def reconfigure_start(message: Message, state: FSMContext) -> None:
    if not await _ensure_admin(message):
        return
    await state.set_state(AdminStates.waiting_reconfigure)
    await message.answer("Формат: campaign_id batch_size interval_minutes. Например: 1 200 30")


@router.message(AdminStates.waiting_reconfigure)
async def reconfigure_receive(message: Message, state: FSMContext) -> None:
    if not await _ensure_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        await message.answer("Формат: campaign_id batch_size interval_minutes")
        return
    campaign_id, batch_size, interval = map(int, parts)
    async with SessionLocal() as session:
        updated = await AppRepository(session).reconfigure_campaign(campaign_id, batch_size, interval)
    await state.clear()
    await message.answer(f"Перенастроено pending/rescheduled получателей: {updated}", reply_markup=MENU)


@router.message(F.text == "Диалог")
async def dialogue_start(message: Message, state: FSMContext) -> None:
    if not await _ensure_admin(message):
        return
    await state.set_state(AdminStates.waiting_dialog_query)
    await message.answer("Пришли chat_id или username.")


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
