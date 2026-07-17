import asyncio
import logging
from datetime import UTC, datetime
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
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

from app.alerts import send_alert, send_critical_alert
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.logging import configure_logging
from app.monitoring import (
    cache_tech_admin_chat_id,
    cache_tech_status_message_id,
    heartbeat_loop,
    read_cached_tech_admin_chat_id,
    read_cached_tech_status_message_id,
    refresh_tech_admin_chat_cache,
    stop_background_task,
)
from app.repositories import AppRepository
from app.services.admin_views import (
    format_ping_delays,
    parse_ping_delays,
    ping_delays_from_config,
    render_start_html,
    render_stats_rich_html,
)
from app.services.leads import render_leads_csv
from app.services.transcript import render_dialogue_html, split_telegram_html

router = Router()
settings = get_settings()
configure_logging(settings)
logger = logging.getLogger(__name__)

HEALTH_COMPONENTS = ("api", "user_bot", "admin_bot", "ping_worker")


MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="CSV"), KeyboardButton(text="Статистика")],
        [KeyboardButton(text="Диалог"), KeyboardButton(text="Настроить пинги")],
        [KeyboardButton(text="Стоп")],
        [KeyboardButton(text="Отмена")],
    ],
    resize_keyboard=True,
)


class AdminStates(StatesGroup):
    waiting_dialog_query = State()
    waiting_ping_delays = State()


async def _admin_for_identity(username: str | None, chat_id: int) -> tuple[int, str] | None:
    role = settings.admin_role_for_username(username)
    if not role:
        return None
    async with SessionLocal() as session:
        admin_id = await AppRepository(session).ensure_admin_user(username or "", role, chat_id)
    if role == "tech":
        cache_tech_admin_chat_id(settings.tech_admin_chat_cache_path, chat_id)
    return admin_id, role


async def _ensure_admin(message: Message) -> tuple[int, str] | None:
    username = message.from_user.username if message.from_user else None
    admin = await _admin_for_identity(username, message.chat.id)
    if not admin:
        await message.answer(
            "Нет доступа. Укажи TECH_ADMIN_USERNAME/BUSINESS_ADMIN_USERNAME в .env."
        )
    return admin


def _status_icon(status: str) -> str:
    return "✅" if status == "ok" else "🚨"


async def _render_tech_status_message() -> str:
    now = datetime.now(UTC)
    database_ok = True
    components: dict[str, dict[str, str | None]] = {}
    try:
        async with SessionLocal() as session:
            components = await AppRepository(session).get_service_health(
                HEALTH_COMPONENTS,
                settings.heartbeat_stale_seconds,
            )
    except Exception:
        database_ok = False
        logger.warning("failed to render pinned tech status from database", exc_info=True)

    lines = [
        "<b>Siemensbot status</b>",
        f"Env: <code>{escape(settings.app_env)}</code>",
        f"Updated: <code>{now.strftime('%Y-%m-%d %H:%M:%S')} UTC</code>",
        "",
        "VPS/admin bot: ✅ alive",
        f"Database/heartbeats: {'✅ ok' if database_ok else '🚨 unavailable'}",
    ]
    if components:
        lines.append("")
        for component in HEALTH_COMPONENTS:
            item = components.get(component) or {"status": "stale", "updated_at": None}
            status = str(item["status"])
            updated_at = item.get("updated_at") or "never"
            lines.append(
                f"{_status_icon(status)} <code>{escape(component)}</code>: "
                f"{escape(status)} · <code>{escape(str(updated_at))}</code>"
            )
    lines.extend(
        [
            "",
            "If this message is older than 2-3 minutes, check the VPS manually.",
        ]
    )
    return "\n".join(lines)


async def _upsert_pinned_tech_status(bot: Bot) -> None:
    chat_id = read_cached_tech_admin_chat_id(settings.tech_admin_chat_cache_path)
    if chat_id is None:
        chat_id = await refresh_tech_admin_chat_cache(settings)
    if chat_id is None:
        logger.warning("tech status pin skipped: tech admin chat id is unavailable")
        return

    text = await _render_tech_status_message()
    message_id = read_cached_tech_status_message_id(settings.tech_status_message_cache_path)
    if message_id is not None:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="HTML")
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            logger.warning("failed to edit pinned tech status; creating a new one", exc_info=True)
        except Exception:
            logger.warning("failed to edit pinned tech status; creating a new one", exc_info=True)

    message = await bot.send_message(chat_id, text, parse_mode="HTML")
    cache_tech_status_message_id(settings.tech_status_message_cache_path, message.message_id)
    try:
        await bot.pin_chat_message(chat_id, message.message_id, disable_notification=True)
    except Exception:
        logger.warning("failed to pin tech status message", exc_info=True)


async def tech_status_loop(bot: Bot) -> None:
    delay = max(30, settings.tech_status_update_seconds)
    while True:
        try:
            await _upsert_pinned_tech_status(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("tech status update failed", exc_info=True)
        await asyncio.sleep(delay)


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


@router.message(Command("ping_settings"))
@router.message(F.text == "Настроить пинги")
async def ping_settings_start(message: Message, state: FSMContext) -> None:
    if not await _ensure_admin(message):
        return
    async with SessionLocal() as session:
        config = await AppRepository(session).get_app_config()
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
    await message.answer(
        f"Интервалы пингов сохранены: {format_ping_delays(delays)}.", reply_markup=MENU
    )


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


@router.message(Command("test_alert"))
async def test_alert(message: Message) -> None:
    admin = await _ensure_admin(message)
    if not admin:
        return
    _admin_id, role = admin
    if role != "tech":
        await message.answer("Команда доступна только техническому администратору.")
        return
    async with SessionLocal() as session:
        delivered = await send_alert(
            session,
            settings,
            "info",
            "monitoring_test",
            "Тестовое техническое уведомление",
            {"requested_by": settings.tech_admin_username_normalized},
            force=True,
        )
    if not delivered:
        await message.answer("Не удалось доставить тестовый алёрт. Проверь логи.")


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


@router.errors()
async def errors(event) -> None:
    logger.exception("admin bot error: %s", event.exception)
    await send_critical_alert(None, settings, "admin_bot", str(event.exception), {})


async def main() -> None:
    if not settings.admin_bot_token:
        raise RuntimeError("ADMIN_BOT_TOKEN is required")
    await refresh_tech_admin_chat_cache(settings)
    bot = Bot(settings.admin_bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    heartbeat_task = asyncio.create_task(heartbeat_loop("admin_bot", settings))
    tech_status_task = asyncio.create_task(tech_status_loop(bot))
    try:
        await dp.start_polling(bot)
    except Exception as exc:
        await send_critical_alert(None, settings, "admin_bot", str(exc), {})
        raise
    finally:
        await stop_background_task(heartbeat_task)
        await stop_background_task(tech_status_task)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
