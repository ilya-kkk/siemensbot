import asyncio
import logging
import re
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
    cache_business_admin_chat_id,
    cache_business_status_message_id,
    cache_tech_admin_chat_id,
    cache_tech_status_message_id,
    heartbeat_loop,
    read_cached_business_admin_chat_id,
    read_cached_business_status_message_id,
    read_cached_tech_admin_chat_id,
    read_cached_tech_status_message_id,
    refresh_tech_admin_chat_cache,
    stop_background_task,
)
from app.repositories import AppRepository
from app.services.admin_views import (
    format_ping_delays,
    parse_growth_alert_threshold,
    parse_ping_delays,
    parse_referral_source_title,
    ping_delays_from_config,
    render_admin_summary_html,
    render_start_html,
    render_stats_rich_html,
    render_users_rich_html,
)
from app.services.dialogue_report import render_dialogues_report_html
from app.services.leads import render_leads_xlsx
from app.services.transcript import render_dialogue_html, split_telegram_html

router = Router()
settings = get_settings()
configure_logging(settings)
logger = logging.getLogger(__name__)

HEALTH_COMPONENTS = ("api", "user_bot", "admin_bot", "ping_worker", "google_sheets_worker")
DIALOG_SHORTCUT_RE = re.compile(r"^/dialog_([1-9]\d*)(?:@[A-Za-z0-9_]+)?$")


MENU = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="Отчёт"),
            KeyboardButton(text="Статистика"),
            KeyboardButton(text="Юзеры"),
        ],
        [
            KeyboardButton(text="Диалог"),
            KeyboardButton(text="Линк"),
            KeyboardButton(text="Пинги"),
        ],
        [
            KeyboardButton(text="Алерт"),
            KeyboardButton(text="Стоп"),
            KeyboardButton(text="Отмена"),
        ],
    ],
    resize_keyboard=True,
)


class AdminStates(StatesGroup):
    waiting_dialog_query = State()
    waiting_ping_delays = State()
    waiting_growth_alert_threshold = State()
    waiting_referral_source_title = State()


async def _admin_for_identity(username: str | None, chat_id: int) -> tuple[int, str] | None:
    role = settings.admin_role_for_username(username)
    if not role:
        return None
    async with SessionLocal() as session:
        admin_id = await AppRepository(session).ensure_admin_user(username or "", role, chat_id)
    _cache_admin_chat_id(role, chat_id)
    return admin_id, role


def _cache_admin_chat_id(role: str, chat_id: int) -> None:
    if role == "tech":
        cache_tech_admin_chat_id(settings.tech_admin_chat_cache_path, chat_id)
    else:
        cache_business_admin_chat_id(settings.business_admin_chat_cache_path, chat_id)


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
    summary: dict[str, int] | None = None
    summary_updated_at: datetime | None = None
    components: dict[str, dict[str, str | None]] = {}
    try:
        async with SessionLocal() as session:
            repository = AppRepository(session)
            summary = await repository.get_admin_summary()
            components = await repository.get_service_health(
                HEALTH_COMPONENTS,
                settings.heartbeat_stale_seconds,
            )
            summary_updated_at = datetime.now(UTC)
    except Exception:
        database_ok = False
        logger.warning("failed to render pinned tech status from database", exc_info=True)

    lines = [
        render_admin_summary_html(summary, summary_updated_at),
        "",
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


async def _render_business_status_message() -> str:
    async with SessionLocal() as session:
        summary = await AppRepository(session).get_admin_summary()
    return render_admin_summary_html(summary, datetime.now(UTC))


def _status_cache_paths(role: str):
    if role == "tech":
        return settings.tech_admin_chat_cache_path, settings.tech_status_message_cache_path
    return settings.business_admin_chat_cache_path, settings.business_status_message_cache_path


def _read_cached_status_ids(role: str) -> tuple[int | None, int | None]:
    chat_path, message_path = _status_cache_paths(role)
    if role == "tech":
        return (
            read_cached_tech_admin_chat_id(chat_path),
            read_cached_tech_status_message_id(message_path),
        )
    return (
        read_cached_business_admin_chat_id(chat_path),
        read_cached_business_status_message_id(message_path),
    )


def _cache_status_message_id(role: str, message_id: int) -> None:
    _chat_path, message_path = _status_cache_paths(role)
    if role == "tech":
        cache_tech_status_message_id(message_path, message_id)
    else:
        cache_business_status_message_id(message_path, message_id)


async def _pin_status_message(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.pin_chat_message(chat_id, message_id, disable_notification=True)
    except Exception:
        logger.warning("failed to pin %s admin status message", chat_id, exc_info=True)


async def _upsert_pinned_admin_status(
    bot: Bot,
    role: str,
    *,
    chat_id: int | None = None,
    ensure_pinned: bool = False,
) -> None:
    cached_chat_id, message_id = _read_cached_status_ids(role)
    chat_id = chat_id if chat_id is not None else cached_chat_id
    if chat_id is None and role == "tech":
        chat_id = await refresh_tech_admin_chat_cache(settings)
    if chat_id is None:
        logger.debug("%s status pin skipped: admin chat id is unavailable", role)
        return

    text = (
        await _render_tech_status_message()
        if role == "tech"
        else await _render_business_status_message()
    )
    if message_id is not None:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="HTML")
            if ensure_pinned:
                await _pin_status_message(bot, chat_id, message_id)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                if ensure_pinned:
                    await _pin_status_message(bot, chat_id, message_id)
                return
            logger.warning(
                "failed to edit pinned %s status; creating a new one", role, exc_info=True
            )
        except Exception:
            logger.warning(
                "failed to edit pinned %s status; creating a new one", role, exc_info=True
            )

    message = await bot.send_message(chat_id, text, parse_mode="HTML")
    _cache_status_message_id(role, message.message_id)
    await _pin_status_message(bot, chat_id, message.message_id)


async def _upsert_pinned_tech_status(bot: Bot) -> None:
    """Backward-compatible wrapper for the technical status updater."""
    await _upsert_pinned_admin_status(bot, "tech")


async def tech_status_loop(bot: Bot) -> None:
    delay = max(30, settings.tech_status_update_seconds)
    while True:
        for role in ("tech", "business"):
            try:
                await _upsert_pinned_admin_status(bot, role)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("%s status update failed", role, exc_info=True)
        await asyncio.sleep(delay)


def _notification_retry_seconds(attempt_count: int) -> int:
    return min(300, 5 * (2 ** min(max(attempt_count - 1, 0), 6)))


async def _deliver_admin_notifications(bot: Bot) -> None:
    async with SessionLocal() as session:
        deliveries = await AppRepository(session).claim_admin_notification_deliveries()
    for delivery in deliveries:
        try:
            await bot.send_message(delivery["chat_id"], delivery["message_text"])
        except Exception as exc:
            logger.warning(
                "failed to deliver admin notification %s",
                delivery["id"],
                exc_info=True,
            )
            async with SessionLocal() as session:
                await AppRepository(session).fail_admin_notification_delivery(
                    delivery["id"],
                    delivery["claim_token"],
                    str(exc),
                    _notification_retry_seconds(delivery["attempt_count"]),
                )
        else:
            async with SessionLocal() as session:
                await AppRepository(session).complete_admin_notification_delivery(
                    delivery["id"], delivery["claim_token"]
                )


async def admin_notification_loop(bot: Bot) -> None:
    while True:
        try:
            await _deliver_admin_notifications(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("admin notification delivery loop failed", exc_info=True)
        await asyncio.sleep(2)


async def _growth_alert_recipients() -> list[dict]:
    async with SessionLocal() as session:
        recipients = await AppRepository(session).get_growth_alert_recipients(
            settings.tech_admin_username_normalized,
            settings.business_admin_username_normalized,
        )
    if {recipient["role"] for recipient in recipients} != {"tech", "business"}:
        return []
    return recipients


async def _get_user_bot_username() -> str:
    if not settings.user_bot_token:
        raise RuntimeError("USER_BOT_TOKEN is required")
    bot = Bot(settings.user_bot_token)
    try:
        me = await bot.get_me()
    finally:
        await bot.session.close()
    username = (me.username or "").strip().lstrip("@")
    if not username:
        raise RuntimeError("user bot username is unavailable")
    return username


@router.message(CommandStart())
async def start(message: Message, bot: Bot) -> None:
    try:
        admin = await _ensure_admin(message)
    except Exception:
        username = message.from_user.username if message.from_user else None
        role = settings.admin_role_for_username(username)
        if role is None:
            raise
        logger.warning(
            "database unavailable while registering %s admin during /start", role, exc_info=True
        )
        try:
            _cache_admin_chat_id(role, message.chat.id)
        except Exception:
            logger.warning("failed to cache %s admin chat during /start", role, exc_info=True)
        admin = (0, role)
    if not admin:
        return
    _admin_id, role = admin
    try:
        await _upsert_pinned_admin_status(
            bot,
            role,
            chat_id=message.chat.id,
            ensure_pinned=True,
        )
    except Exception:
        logger.warning("failed to refresh %s status during /start", role, exc_info=True)
    await message.answer(render_start_html(), reply_markup=MENU, parse_mode="HTML")


@router.message(Command("leads"))
@router.message(F.text == "Таблица")
@router.message(F.text == "CSV")
async def download_leads(message: Message) -> None:
    if not await _ensure_admin(message):
        return
    async with SessionLocal() as session:
        rows = await AppRepository(session).get_leads_for_export()

    xlsx_bytes = render_leads_xlsx(rows)
    filename = f"leads_{datetime.now(UTC).strftime('%Y-%m-%d_%H-%M-%S')}.xlsx"
    await message.answer_document(
        BufferedInputFile(xlsx_bytes, filename=filename),
        caption=f"Лидов: {len(rows)}",
        reply_markup=MENU,
    )


@router.message(Command("report"))
@router.message(F.text == "Отчёт")
async def download_dialogues_report(message: Message) -> None:
    if not await _ensure_admin(message):
        return
    async with SessionLocal() as session:
        repository = AppRepository(session)
        unanswered_users = await repository.get_unanswered_start_users_for_report()
        dialogues = await repository.get_dialogues_for_report()

    generated_at = datetime.now(UTC)
    report = render_dialogues_report_html(
        dialogues,
        unanswered_users=unanswered_users,
        generated_at=generated_at,
    )
    filename = f"dialogues_{generated_at.strftime('%Y-%m-%d_%H-%M-%S')}.html"
    message_count = sum(len(dialogue.get("messages") or []) for dialogue in dialogues)
    await message.answer_document(
        BufferedInputFile(report, filename=filename),
        caption=(
            f"Диалогов: {len(dialogues)} · сообщений: {message_count} · "
            f"без ответа: {len(unanswered_users)}"
        ),
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


@router.message(Command("users"))
@router.message(F.text == "Юзеры")
async def users(message: Message) -> None:
    if not await _ensure_admin(message):
        return
    async with SessionLocal() as session:
        data = await AppRepository(session).get_users_by_start_day()
    for html in render_users_rich_html(data):
        await message.answer_rich(
            InputRichMessage(html=html, skip_entity_detection=False),
            reply_markup=MENU,
        )


@router.message(Command("generate_link"))
@router.message(F.text.in_({"Линк", "Сгенерировать линк"}))
async def generate_referral_link_start(message: Message, state: FSMContext) -> None:
    if not await _ensure_admin(message):
        return
    await state.set_state(AdminStates.waiting_referral_source_title)
    await message.answer(
        "Пришли название канала или источника, например: Instagram Reels.",
        reply_markup=MENU,
    )


@router.message(AdminStates.waiting_referral_source_title)
async def generate_referral_link_receive(message: Message, state: FSMContext) -> None:
    admin = await _ensure_admin(message)
    if not admin:
        return
    if message.text == "Отмена":
        await state.clear()
        await message.answer("Действие отменено.", reply_markup=MENU)
        return
    try:
        title = parse_referral_source_title(message.text)
    except ValueError:
        await message.answer(
            "Нужно непустое название до 200 символов. Либо нажми «Отмена».",
            reply_markup=MENU,
        )
        return

    try:
        user_bot_username = await _get_user_bot_username()
    except Exception:
        logger.warning("failed to resolve user bot username for referral link", exc_info=True)
        await message.answer(
            "Не удалось получить username пользовательского бота. Проверь USER_BOT_TOKEN.",
            reply_markup=MENU,
        )
        return

    admin_id, _role = admin
    created_by_username = message.from_user.username if message.from_user else None
    async with SessionLocal() as session:
        source = await AppRepository(session).create_referral_source(
            title,
            admin_id,
            created_by_username or "unknown",
        )

    link = f"https://t.me/{user_bot_username}?start={source['source_code']}"
    await state.clear()
    await message.answer(
        "\n".join(
            [
                "Ссылка создана.",
                f"Название: <b>{escape(title)}</b>",
                f"ID: <code>{escape(str(source['source_code']))}</code>",
                f"Ссылка: {escape(link)}",
            ]
        ),
        reply_markup=MENU,
        parse_mode="HTML",
    )


@router.message(F.text.regexp(DIALOG_SHORTCUT_RE))
async def dialogue_shortcut(message: Message, state: FSMContext) -> None:
    if not await _ensure_admin(message):
        return
    match = DIALOG_SHORTCUT_RE.fullmatch(message.text or "")
    if match is None:
        return
    user_record_id = int(match.group(1))
    await state.clear()
    async with SessionLocal() as session:
        rows = await AppRepository(session).get_user_messages_by_id(user_record_id)
    html = render_dialogue_html(rows)
    for chunk in split_telegram_html(html):
        await message.answer(chunk, parse_mode="HTML")


@router.message(Command("cancel"))
@router.message(F.text == "Отмена")
async def cancel(message: Message, state: FSMContext) -> None:
    if not await _ensure_admin(message):
        return
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=MENU)


@router.message(Command("set_alert"))
@router.message(F.text.in_({"Алерт", "Установить алерт"}))
async def growth_alert_start(message: Message, state: FSMContext) -> None:
    if not await _ensure_admin(message):
        return
    if not await _growth_alert_recipients():
        await message.answer(
            "Не могу установить алерт: технический и бизнес-администратор должны "
            "хотя бы один раз нажать Start в админ-боте.",
            reply_markup=MENU,
        )
        return
    await state.set_state(AdminStates.waiting_growth_alert_threshold)
    await message.answer(
        "Через сколько новых пользователей должен сработать алерт? "
        "Пришли одно положительное целое число, например: 100.",
        reply_markup=MENU,
    )


@router.message(AdminStates.waiting_growth_alert_threshold)
async def growth_alert_receive(message: Message, state: FSMContext, bot: Bot) -> None:
    admin = await _ensure_admin(message)
    if not admin:
        return
    try:
        threshold = parse_growth_alert_threshold(message.text)
    except ValueError:
        await message.answer(
            "Нужно одно положительное целое число, например: 100. "
            "Либо нажми «Отмена».",
            reply_markup=MENU,
        )
        return

    recipients = await _growth_alert_recipients()
    if not recipients:
        await state.clear()
        await message.answer(
            "Алерт не установлен: технический и бизнес-администратор должны "
            "хотя бы один раз нажать Start в админ-боте.",
            reply_markup=MENU,
        )
        return

    admin_id, _role = admin
    username = message.from_user.username if message.from_user else None
    async with SessionLocal() as session:
        await AppRepository(session).set_user_growth_alert(
            threshold,
            admin_id,
            username or "unknown",
            recipients,
        )
    await state.clear()
    await _deliver_admin_notifications(bot)


@router.message(Command("ping_settings"))
@router.message(F.text.in_({"Пинги", "Настроить пинги"}))
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
    notification_task = asyncio.create_task(admin_notification_loop(bot))
    try:
        await dp.start_polling(bot)
    except Exception as exc:
        await send_critical_alert(None, settings, "admin_bot", str(exc), {})
        raise
    finally:
        await stop_background_task(heartbeat_task)
        await stop_background_task(tech_status_task)
        await stop_background_task(notification_task)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
