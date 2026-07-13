import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SendMessage

from app.ai.openrouter import OpenRouterError, PingResult
from app.services.telegram_errors import classify_telegram_error
from app.workers import ping_worker


class _SessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakeRepository:
    def __init__(self) -> None:
        self.stopped = False
        self.validated: dict[str, object] | None = {
            "chat_id": 700,
            "ping_number": 1,
            "offer_shown": False,
            "offer_url": "https://example.com/form",
        }
        self.saved_ai_requests: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.completed: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.released: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.delivery_failures: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def is_client_bot_stopped(self) -> bool:
        return self.stopped

    async def create_ping_trigger(self, *_args: object) -> int:
        return 50

    async def get_transcript_for_user(self, *_args: object, **_kwargs: object) -> str:
        return "outgoing: В какой нише проект?"

    async def get_user_snapshot(self, _telegram_user_id: int) -> dict[str, object]:
        return {"id": 7, "funnel_stage": "started"}

    async def save_ai_request(self, *args: object, **kwargs: object) -> int:
        self.saved_ai_requests.append((args, kwargs))
        return 90

    async def set_pending_ping_ai_request(self, *_args: object) -> bool:
        return True

    async def validate_ping_claim(self, *_args: object) -> dict[str, object] | None:
        return self.validated

    async def complete_ping_send(self, *args: object, **kwargs: object) -> bool:
        self.completed.append((args, kwargs))
        return True

    async def release_ping_claim(self, *args: object, **kwargs: object) -> bool:
        self.released.append((args, kwargs))
        return True

    async def fail_ping_delivery(self, *args: object, **kwargs: object) -> bool:
        self.delivery_failures.append((args, kwargs))
        return True

    async def get_or_create_offer_token(
        self,
        _telegram_user_id: int,
        **_kwargs: object,
    ) -> str:
        return "tracked-token"

    async def get_app_config(self, _default_offer_url: str) -> dict[str, object]:
        return {"offer_url": "https://current.example/form"}


class _SentMessage:
    message_id = 501
    date = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)

    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return {"message_id": self.message_id, "text": "Продолжим?"}


def _settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "openrouter_model": "test-model",
        "test_drive_url": "https://fallback.example/form",
        "public_base_url": "https://bot.example",
        "ping_worker_retry_seconds": 300,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _claim(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "telegram_user_id": 7,
        "chat_id": 700,
        "ping_number": 1,
        "claim_token": "00000000-0000-0000-0000-000000000007",
        "anchor_at": "2026-07-13T10:00:00+00:00",
        "idle_minutes": 120,
        "offer_shown": False,
        "offer_url": "https://example.com/form",
        "ping_pending_ai_request_id": None,
        "pending_response_payload": None,
        "ping_1_delay_minutes": 120,
        "ping_2_delay_minutes": 1440,
        "ping_3_delay_minutes": 4320,
    }
    values.update(overrides)
    return values


@pytest.fixture
def fake_runtime(monkeypatch: pytest.MonkeyPatch) -> _FakeRepository:
    repository = _FakeRepository()
    monkeypatch.setattr(ping_worker, "SessionLocal", lambda: _SessionContext())
    monkeypatch.setattr(ping_worker, "AppRepository", lambda _session: repository)
    monkeypatch.setattr(ping_worker, "send_critical_alert", AsyncMock())
    return repository


@pytest.mark.asyncio
async def test_fresh_ping_persists_ai_and_outgoing_message(fake_runtime: _FakeRepository) -> None:
    client = AsyncMock()
    client.generate_ping.return_value = PingResult(
        text="Вернемся к вопросу о нише?",
        usage={"total_tokens": 20, "cost": 0.001},
        request_payload={"model": "test-model"},
        response_payload={"choices": []},
    )
    bot = AsyncMock()
    bot.send_message.return_value = _SentMessage()

    sent = await ping_worker._process_claim(bot, client, _settings(), _claim())

    assert sent is True
    client.generate_ping.assert_awaited_once()
    bot.send_message.assert_awaited_once_with(700, "Вернемся к вопросу о нише?", reply_markup=None)
    saved_args = fake_runtime.saved_ai_requests[0][0]
    saved_kwargs = fake_runtime.saved_ai_requests[0][1]
    assert saved_args[2:5] == ("ping", "test-model", "success")
    assert saved_kwargs["commit"] is False
    completed_args = fake_runtime.completed[0][0]
    assert completed_args[2:7] == (1, 90, "Вернемся к вопросу о нише?", 501, {
        "event": "ping",
        "ping_number": 1,
        "telegram": {"message_id": 501, "text": "Продолжим?"},
    })
    assert fake_runtime.completed[0][1]["sent_at"] == _SentMessage.date


@pytest.mark.asyncio
async def test_telegram_429_reuses_pending_ai_without_new_llm_call(
    fake_runtime: _FakeRepository,
) -> None:
    payload = {
        "choices": [{"message": {"content": json.dumps({"text": "Продолжим разбор?"})}}]
    }
    client = AsyncMock()
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramRetryAfter(
        method=SendMessage(chat_id=700, text="Продолжим разбор?"),
        message="Too Many Requests",
        retry_after=30,
    )

    sent = await ping_worker._process_claim(
        bot,
        client,
        _settings(),
        _claim(ping_pending_ai_request_id=90, pending_response_payload=payload),
    )

    assert sent is False
    client.generate_ping.assert_not_awaited()
    failure_kwargs = fake_runtime.delivery_failures[0][1]
    assert failure_kwargs["preserve_pending"] is True
    assert failure_kwargs["user_status"] is None
    assert failure_kwargs["retry_at"] is not None


@pytest.mark.asyncio
async def test_permanent_telegram_400_keeps_pending_text_and_alerts(
    fake_runtime: _FakeRepository,
) -> None:
    payload = {
        "choices": [{"message": {"content": json.dumps({"text": "Продолжим разбор?"})}}]
    }
    client = AsyncMock()
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramBadRequest(
        method=SendMessage(chat_id=700, text="Продолжим разбор?"),
        message="Bad Request: message is too long",
    )

    sent = await ping_worker._process_claim(
        bot,
        client,
        _settings(),
        _claim(ping_pending_ai_request_id=90, pending_response_payload=payload),
    )

    assert sent is False
    failure_kwargs = fake_runtime.delivery_failures[0][1]
    assert failure_kwargs["preserve_pending"] is True


@pytest.mark.asyncio
async def test_invalidated_claim_is_not_sent(fake_runtime: _FakeRepository) -> None:
    fake_runtime.validated = None
    payload = {
        "choices": [{"message": {"content": json.dumps({"text": "Продолжим?"})}}]
    }
    bot = AsyncMock()
    client = AsyncMock()

    sent = await ping_worker._process_claim(
        bot,
        client,
        _settings(),
        _claim(ping_pending_ai_request_id=90, pending_response_payload=payload),
    )

    assert sent is False
    bot.send_message.assert_not_awaited()
    assert fake_runtime.released[0][1]["preserve_pending"] is True


@pytest.mark.asyncio
async def test_ping_after_offer_repeats_tracked_button(fake_runtime: _FakeRepository) -> None:
    fake_runtime.validated = {
        "chat_id": 700,
        "ping_number": 1,
        "offer_shown": True,
        "offer_url": "https://example.com/form",
    }
    client = AsyncMock()
    client.generate_ping.return_value = PingResult(
        text="Если готов, кнопка всё ещё ниже.",
        usage=None,
        request_payload={},
        response_payload={},
    )
    bot = AsyncMock()
    bot.send_message.return_value = _SentMessage()

    sent = await ping_worker._process_claim(
        bot,
        client,
        _settings(),
        _claim(offer_shown=True),
    )

    assert sent is True
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].url == "https://bot.example/r/tracked-token"
    assert fake_runtime.completed[0][1]["message_type"] == "button"


@pytest.mark.asyncio
async def test_failed_llm_request_is_saved_and_retried(fake_runtime: _FakeRepository) -> None:
    client = AsyncMock()
    client.generate_ping.side_effect = OpenRouterError(
        "provider unavailable",
        request_payload={"model": "test-model"},
        response_payload={"status_code": 503},
    )
    bot = AsyncMock()

    sent = await ping_worker._process_claim(bot, client, _settings(), _claim())

    assert sent is False
    bot.send_message.assert_not_awaited()
    saved_args = fake_runtime.saved_ai_requests[0][0]
    assert saved_args[2:5] == ("ping", "test-model", "failed")
    assert fake_runtime.released[0][1]["preserve_pending"] is False
    assert fake_runtime.released[0][0][2] is not None


@pytest.mark.asyncio
async def test_stop_flag_after_generation_prevents_delivery(fake_runtime: _FakeRepository) -> None:
    fake_runtime.is_client_bot_stopped = AsyncMock(side_effect=[False, True])  # type: ignore[method-assign]
    client = AsyncMock()
    client.generate_ping.return_value = PingResult(
        text="Продолжим?",
        usage=None,
        request_payload={},
        response_payload={},
    )
    bot = AsyncMock()

    sent = await ping_worker._process_claim(bot, client, _settings(), _claim())

    assert sent is False
    bot.send_message.assert_not_awaited()
    assert fake_runtime.released[-1][1]["preserve_pending"] is True


def test_ping_delivery_error_classification() -> None:
    assert classify_telegram_error(403, "Forbidden").user_status == "blocked"
    assert classify_telegram_error(404, "Not found").user_status == "invalid"
    assert classify_telegram_error(400, "Bad Request: chat not found").user_status == "invalid"
    generic_bad_request = classify_telegram_error(400, "Bad Request: can't parse entities")
    assert generic_bad_request.user_status is None
    assert generic_bad_request.is_critical is True
    retry = classify_telegram_error(429, "Too Many Requests", {"retry_after": 17})
    assert retry.retry_after_seconds == 17
    assert classify_telegram_error(401, "Unauthorized").is_critical is True


def test_legacy_pending_payload_obeys_telegram_length_limit() -> None:
    assert ping_worker._extract_pending_ping_text({"text": "x" * 4097}) is None
