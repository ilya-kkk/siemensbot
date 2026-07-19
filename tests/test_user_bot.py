from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramAPIError

from app.ai.openrouter import OpenRouterError
from app.bots import user_bot


def _message(chat_id: int = 100, text: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(
            id=200,
            username="admin",
            first_name="Анна",
            last_name=None,
        ),
        voice=SimpleNamespace(file_id="voice-file"),
        text=text,
        caption=None,
        date=None,
        message_id=300,
        answer=AsyncMock(),
        model_dump=lambda **kwargs: {"voice": {"file_id": "voice-file"}},
    )


def test_offer_button_is_a_callback_without_external_url() -> None:
    button = user_bot._offer_markup().inline_keyboard[0][0]

    assert button.url is None
    assert button.callback_data == user_bot.OFFER_CALLBACK_DATA


@pytest.mark.asyncio
async def test_test_offer_callback_does_not_touch_production_data() -> None:
    callback = SimpleNamespace(answer=AsyncMock())

    await user_bot.register_test_lead(callback)

    callback.answer.assert_awaited_once_with("Тестовая заявка принята")


@pytest.mark.asyncio
async def test_lead_callback_marks_user_then_analyzes_full_dialogue(monkeypatch) -> None:
    repo = SimpleNamespace(
        record_lead_click=AsyncMock(return_value=10),
        get_message_id_for_telegram_message=AsyncMock(return_value=20),
    )

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    callback_message = SimpleNamespace(
        chat=SimpleNamespace(id=100),
        message_id=300,
        answer=AsyncMock(),
        edit_reply_markup=AsyncMock(),
    )
    callback = SimpleNamespace(
        message=callback_message,
        from_user=SimpleNamespace(id=200),
        answer=AsyncMock(),
    )
    analyze = AsyncMock(return_value=False)
    monkeypatch.setattr(user_bot, "SessionLocal", SessionContext)
    monkeypatch.setattr(user_bot, "AppRepository", lambda _session: repo)
    monkeypatch.setattr(user_bot, "_ensure_user_analysis", analyze)

    await user_bot.register_lead(callback)

    callback.answer.assert_awaited_once_with()
    callback_message.answer.assert_awaited_once_with(
        "Ваша заявка принята, скоро с вами свяжется менеджер."
    )
    repo.record_lead_click.assert_awaited_once_with(100, 200)
    repo.get_message_id_for_telegram_message.assert_awaited_once_with(10, 300)
    analyze.assert_awaited_once()
    assert analyze.await_args.args[:2] == (10, 20)
    callback_message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_transcribe_voice_downloads_in_memory_without_echo(monkeypatch) -> None:
    downloaded: list[tuple[str, object]] = []

    async def download(file_id: str, destination) -> None:
        downloaded.append((file_id, destination))
        destination.write(b"ogg-bytes")

    class FakeOpenRouterClient:
        def __init__(self, settings) -> None:
            pass

        async def transcribe_ogg(self, audio_bytes: bytes) -> str:
            assert audio_bytes == b"ogg-bytes"
            return "Русская расшифровка"

    monkeypatch.setattr(user_bot, "OpenRouterClient", FakeOpenRouterClient)
    message = _message()
    bot = SimpleNamespace(download=download)

    result = await user_bot._transcribe_voice(bot, message)

    assert result == "Русская расшифровка"
    assert downloaded[0][0] == "voice-file"
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_transcribe_voice_reports_download_error_once(monkeypatch) -> None:
    error = TelegramAPIError(method=None, message="download failed")
    monkeypatch.setattr(
        user_bot,
        "_download_voice_bytes",
        AsyncMock(side_effect=error),
    )
    message = _message()

    result = await user_bot._transcribe_voice(object(), message)

    assert result is None
    message.answer.assert_awaited_once_with(user_bot.VOICE_TRANSCRIPTION_ERROR)


@pytest.mark.asyncio
@pytest.mark.parametrize("transcription", ["", "   "])
async def test_transcribe_voice_reports_empty_text_once(monkeypatch, transcription: str) -> None:
    class FakeOpenRouterClient:
        def __init__(self, settings) -> None:
            pass

        async def transcribe_ogg(self, audio_bytes: bytes) -> str:
            return transcription.strip()

    monkeypatch.setattr(user_bot, "_download_voice_bytes", AsyncMock(return_value=b"ogg"))
    monkeypatch.setattr(user_bot, "OpenRouterClient", FakeOpenRouterClient)
    message = _message()

    result = await user_bot._transcribe_voice(object(), message)

    assert result is None
    message.answer.assert_awaited_once_with(user_bot.VOICE_TRANSCRIPTION_ERROR)


@pytest.mark.asyncio
async def test_transcribe_voice_reports_openrouter_error_once(monkeypatch) -> None:
    class FakeOpenRouterClient:
        def __init__(self, settings) -> None:
            pass

        async def transcribe_ogg(self, audio_bytes: bytes) -> str:
            raise OpenRouterError("stt failed")

    monkeypatch.setattr(user_bot, "_download_voice_bytes", AsyncMock(return_value=b"ogg"))
    monkeypatch.setattr(user_bot, "OpenRouterClient", FakeOpenRouterClient)
    message = _message()

    result = await user_bot._transcribe_voice(object(), message)

    assert result is None
    message.answer.assert_awaited_once_with(user_bot.VOICE_TRANSCRIPTION_ERROR)


@pytest.mark.asyncio
async def test_voice_message_uses_production_text_pipeline_without_echo(monkeypatch) -> None:
    message = _message(chat_id=401)
    user_bot.test_sessions.pop(message.chat.id, None)
    transcribe = AsyncMock(return_value="Текст из голоса")
    dialogue = AsyncMock()
    test_dialogue = AsyncMock()
    monkeypatch.setattr(user_bot, "_client_bot_stopped", AsyncMock(return_value=False))
    monkeypatch.setattr(user_bot, "_lead_user_id_for_message", AsyncMock(return_value=None))
    monkeypatch.setattr(user_bot, "_transcribe_voice", transcribe)
    monkeypatch.setattr(user_bot, "_handle_dialogue_message", dialogue)
    monkeypatch.setattr(user_bot, "_handle_test_message", test_dialogue)

    await user_bot.voice_message(message, object())

    dialogue.assert_awaited_once_with(message, "Текст из голоса")
    test_dialogue.assert_not_awaited()
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_message_uses_test_session_without_production_pipeline(monkeypatch) -> None:
    message = _message(chat_id=402)
    user_bot.test_sessions[message.chat.id] = ["outgoing: Привет"]
    transcribe = AsyncMock(return_value="Тестовый голос")
    dialogue = AsyncMock()
    test_dialogue = AsyncMock()
    monkeypatch.setattr(user_bot, "_is_test_admin", lambda user: True)
    monkeypatch.setattr(user_bot, "_transcribe_voice", transcribe)
    monkeypatch.setattr(user_bot, "_handle_dialogue_message", dialogue)
    monkeypatch.setattr(user_bot, "_handle_test_message", test_dialogue)
    try:
        await user_bot.voice_message(message, object())
    finally:
        user_bot.test_sessions.pop(message.chat.id, None)

    test_dialogue.assert_awaited_once_with(message, "Тестовый голос")
    dialogue.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_message_does_not_download_when_bot_is_stopped(monkeypatch) -> None:
    message = _message(chat_id=403)
    user_bot.test_sessions.pop(message.chat.id, None)
    transcribe = AsyncMock()
    monkeypatch.setattr(user_bot, "_client_bot_stopped", AsyncMock(return_value=True))
    monkeypatch.setattr(user_bot, "_transcribe_voice", transcribe)

    await user_bot.voice_message(message, object())

    transcribe.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_message_for_lead_skips_transcription_and_ai(monkeypatch) -> None:
    message = _message(chat_id=404)
    sent = SimpleNamespace(message_id=301, model_dump=lambda **kwargs: {"text": "sent"})
    message.answer = AsyncMock(return_value=sent)
    repo = SimpleNamespace(log_message=AsyncMock())

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    transcribe = AsyncMock()
    dialogue = AsyncMock()
    monkeypatch.setattr(user_bot, "_client_bot_stopped", AsyncMock(return_value=False))
    monkeypatch.setattr(user_bot, "_lead_user_id_for_message", AsyncMock(return_value=10))
    monkeypatch.setattr(
        user_bot,
        "_register_incoming_message",
        AsyncMock(return_value=(10, 20)),
    )
    monkeypatch.setattr(user_bot, "_transcribe_voice", transcribe)
    monkeypatch.setattr(user_bot, "_handle_dialogue_message", dialogue)
    monkeypatch.setattr(user_bot, "SessionLocal", SessionContext)
    monkeypatch.setattr(user_bot, "AppRepository", lambda _session: repo)

    await user_bot.voice_message(message, object())

    transcribe.assert_not_awaited()
    dialogue.assert_not_awaited()
    message.answer.assert_awaited_once_with(user_bot.LEAD_ALREADY_REGISTERED_TEXT)
    repo.log_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_text_message_still_uses_shared_dialogue_pipeline(monkeypatch) -> None:
    message = _message(text="Обычный текст")
    dialogue = AsyncMock()
    monkeypatch.setattr(user_bot, "_handle_dialogue_message", dialogue)

    await user_bot.text_message(message)

    dialogue.assert_awaited_once_with(message, "Обычный текст")


@pytest.mark.asyncio
async def test_dialogue_message_for_lead_skips_ai_and_returns_fixed_reply(monkeypatch) -> None:
    message = _message(text="Посоветуйте ещё что-нибудь")
    sent = SimpleNamespace(message_id=301, model_dump=lambda **kwargs: {"text": "sent"})
    message.answer = AsyncMock(return_value=sent)
    repo = SimpleNamespace(
        get_user_snapshot=AsyncMock(return_value={"funnel_stage": "lead"}),
        get_transcript_for_user=AsyncMock(),
        log_message=AsyncMock(),
    )

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    openrouter = Mock(side_effect=AssertionError("OpenRouter must not be called for a lead"))
    monkeypatch.setattr(user_bot, "_client_bot_stopped", AsyncMock(return_value=False))
    monkeypatch.setattr(
        user_bot,
        "_register_incoming_message",
        AsyncMock(return_value=(10, 20)),
    )
    monkeypatch.setattr(user_bot, "SessionLocal", SessionContext)
    monkeypatch.setattr(user_bot, "AppRepository", lambda _session: repo)
    monkeypatch.setattr(user_bot, "OpenRouterClient", openrouter)

    await user_bot._handle_dialogue_message(message, message.text)

    openrouter.assert_not_called()
    repo.get_transcript_for_user.assert_not_awaited()
    message.answer.assert_awaited_once_with(user_bot.LEAD_ALREADY_REGISTERED_TEXT)
    repo.log_message.assert_awaited_once_with(
        10,
        "outgoing",
        user_bot.LEAD_ALREADY_REGISTERED_TEXT,
        301,
        {"text": "sent"},
    )


@pytest.mark.asyncio
async def test_register_incoming_voice_stores_transcription_as_text(monkeypatch) -> None:
    repo = SimpleNamespace(
        upsert_telegram_user=AsyncMock(return_value=10),
        log_message=AsyncMock(return_value=20),
    )

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    monkeypatch.setattr(user_bot, "SessionLocal", SessionContext)
    monkeypatch.setattr(user_bot, "AppRepository", lambda session: repo)
    message = _message()

    result = await user_bot._register_incoming_message(
        message,
        "dialogue",
        text_value="Сохранённая расшифровка",
    )

    assert result == (10, 20)
    repo.log_message.assert_awaited_once_with(
        10,
        "incoming",
        "Сохранённая расшифровка",
        300,
        {"voice": {"file_id": "voice-file"}},
        message_type="text",
    )


@pytest.mark.asyncio
async def test_start_checks_growth_alert_before_sending_welcome(monkeypatch) -> None:
    message = _message(text="/start")
    events: list[str] = []

    async def register(*_args, **_kwargs) -> tuple[int, int]:
        events.append("registered")
        return 10, 20

    async def check_alert() -> None:
        events.append("checked")

    async def answer(*_args, **_kwargs):
        events.append("answered")
        return SimpleNamespace(
            message_id=301,
            model_dump=lambda **kwargs: {},
        )

    repo = SimpleNamespace(log_message=AsyncMock())

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    message.answer = AsyncMock(side_effect=answer)
    monkeypatch.setattr(user_bot, "_client_bot_stopped", AsyncMock(side_effect=[False, False]))
    monkeypatch.setattr(user_bot, "_register_incoming_message", register)
    monkeypatch.setattr(user_bot, "_check_user_growth_alert", check_alert)
    monkeypatch.setattr(user_bot, "_reply_if_user_is_lead", AsyncMock(return_value=False))
    monkeypatch.setattr(user_bot, "SessionLocal", SessionContext)
    monkeypatch.setattr(user_bot, "AppRepository", lambda session: repo)

    await user_bot.start(message)

    assert events == ["registered", "checked", "answered"]
    repo.log_message.assert_awaited_once()
