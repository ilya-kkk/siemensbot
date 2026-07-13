import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.ai.openrouter import (
    PING_SCHEMA,
    OpenRouterClient,
    OpenRouterError,
    _is_invalid_niche_answer,
    _is_off_topic_request,
    _sanitize_reply_text,
    parse_ping_response,
)


class FakeOpenRouterClient(OpenRouterClient):
    def __init__(self, reply_text: str, should_send_offer: bool):
        super().__init__(settings=SimpleNamespace(openrouter_model="test-model"))
        self.reply_text = reply_text
        self.model_should_send_offer = should_send_offer
        self.last_payload: dict | None = None

    async def _chat_completion(self, payload: dict) -> dict:
        self.last_payload = payload
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "reply_text": self.reply_text,
                                "should_send_offer": self.model_should_send_offer,
                            }
                        )
                    }
                }
            ],
            "usage": {},
        }


class FakePingOpenRouterClient(OpenRouterClient):
    def __init__(self, response: dict):
        super().__init__(settings=SimpleNamespace(openrouter_model="test-model"))
        self.response = response
        self.last_payload: dict | None = None

    async def _chat_completion(self, payload: dict) -> dict:
        self.last_payload = payload
        return self.response


@pytest.mark.asyncio
async def test_openrouter_error_keeps_full_attempted_payload() -> None:
    client = OpenRouterClient(
        settings=SimpleNamespace(
            openrouter_api_key=None,
            public_base_url=None,
        )
    )
    payload = {"model": "test", "messages": [{"role": "user", "content": "Привет"}]}

    with pytest.raises(OpenRouterError) as captured:
        await client._chat_completion(payload)

    assert captured.value.request_payload == payload
    assert captured.value.response_payload == {}


@pytest.mark.asyncio
async def test_transcribe_ogg_sends_russian_openrouter_request(monkeypatch) -> None:
    calls: list[dict] = []

    class FakeAsyncClient:
        def __init__(self, *, timeout: httpx.Timeout) -> None:
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, **kwargs) -> httpx.Response:
            calls.append({"url": url, **kwargs, "timeout": self.timeout})
            return httpx.Response(200, json={"text": "  Привет, мир  "})

    monkeypatch.setattr("app.ai.openrouter.httpx.AsyncClient", FakeAsyncClient)
    client = OpenRouterClient(
        settings=SimpleNamespace(
            openrouter_api_key="secret",
            openrouter_stt_model="openai/gpt-4o-mini-transcribe",
            public_base_url="https://bot.example",
        )
    )

    transcription = await client.transcribe_ogg(b"ogg-bytes")

    assert transcription == "Привет, мир"
    assert len(calls) == 1
    assert calls[0]["url"] == "https://openrouter.ai/api/v1/audio/transcriptions"
    assert calls[0]["headers"]["Authorization"] == "Bearer secret"
    assert calls[0]["json"] == {
        "model": "openai/gpt-4o-mini-transcribe",
        "input_audio": {
            "data": base64.b64encode(b"ogg-bytes").decode("ascii"),
            "format": "ogg",
        },
        "language": "ru",
    }
    assert calls[0]["timeout"].read == 45


@pytest.mark.asyncio
async def test_transcription_retries_429_and_server_errors(monkeypatch) -> None:
    responses = [
        httpx.Response(429, text="limited"),
        httpx.Response(503, text="unavailable"),
        httpx.Response(200, json={"text": "готово"}),
    ]
    sleep = AsyncMock()

    class FakeAsyncClient:
        def __init__(self, *, timeout: httpx.Timeout) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(self, url: str, **kwargs) -> httpx.Response:
            return responses.pop(0)

    monkeypatch.setattr("app.ai.openrouter.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("app.ai.openrouter.asyncio.sleep", sleep)
    client = OpenRouterClient(
        settings=SimpleNamespace(
            openrouter_api_key="secret",
            openrouter_stt_model="stt-model",
            public_base_url=None,
        )
    )

    assert await client.transcribe_ogg(b"ogg") == "готово"
    assert sleep.await_args_list[0].args == (0.5,)
    assert sleep.await_args_list[1].args == (1.0,)
    assert responses == []


@pytest.mark.asyncio
async def test_transcribe_ogg_rejects_response_without_text(monkeypatch) -> None:
    client = OpenRouterClient(
        settings=SimpleNamespace(openrouter_stt_model="stt-model")
    )
    monkeypatch.setattr(client, "_post_transcription", AsyncMock(return_value={"usage": {}}))

    with pytest.raises(OpenRouterError, match="missing text"):
        await client.transcribe_ogg(b"ogg")


def test_sanitize_reply_text_replaces_long_dash_chars() -> None:
    text = _sanitize_reply_text("Инфобизнес — ниша. Книга – продукт.")

    assert text == "Инфобизнес - ниша. Книга - продукт."
    assert "—" not in text
    assert "–" not in text


def test_sanitize_reply_text_removes_repeated_leading_ack() -> None:
    transcript = "\n".join(
        [
            "incoming: инфобизнес",
            "outgoing: Понятно. Что сейчас продаешь?",
            "incoming: книгу",
        ]
    )

    text = _sanitize_reply_text("Понятно, книга. Какой средний чек?", transcript)

    assert text == "Книга. Какой средний чек?"


def test_detects_code_request_as_off_topic() -> None:
    assert _is_off_topic_request("напиши сортировку пузырьком на Python")
    assert _is_off_topic_request("напиши пост про лето")
    assert not _is_off_topic_request("обучаю Python")
    assert not _is_off_topic_request("что такое тест-драйв")


def test_detects_obviously_invalid_niche_answers() -> None:
    assert _is_invalid_niche_answer("фывапр")
    assert _is_invalid_niche_answer("asdfgh")
    assert _is_invalid_niche_answer("хз")
    assert _is_invalid_niche_answer("любая ниша")
    assert _is_invalid_niche_answer("12345")
    assert not _is_invalid_niche_answer("фитнес")
    assert not _is_invalid_niche_answer("smm")
    assert not _is_invalid_niche_answer("b2b-сервисы")


@pytest.mark.asyncio
async def test_chat_reply_redirects_code_request_without_openrouter_call() -> None:
    client = OpenRouterClient(settings=object())
    transcript = "outgoing: Что сейчас продаешь?"

    decision = await client.chat_reply(transcript, "напиши сортировку пузырьком на Python")

    assert decision.reply_text == "Давай вернемся к разбору проекта. Что сейчас продаешь?"
    assert decision.should_send_offer is False
    assert decision.request_payload["type"] == "local_guard"


@pytest.mark.asyncio
async def test_chat_reply_reasks_niche_for_obviously_invalid_answer() -> None:
    client = OpenRouterClient(settings=object())
    transcript = "outgoing: В какой нише ты работаешь?"

    decision = await client.chat_reply(transcript, "фывапр")

    assert decision.reply_text == (
        "Похоже, это не ниша. Напиши хотя бы примерно, чем занимаешься или с кем работаешь?"
    )
    assert decision.should_send_offer is False
    assert decision.request_payload["type"] == "local_guard"
    assert decision.request_payload["reason"] == "invalid_niche_answer"


@pytest.mark.asyncio
async def test_chat_reply_uses_model_false_offer_decision() -> None:
    client = FakeOpenRouterClient(
        "Тут тест-драйв как раз уместен. Хочешь, отправлю ссылку?",
        should_send_offer=False,
    )

    decision = await client.chat_reply("outgoing: Есть ли понятный путь до покупки?", "скорее хаос")

    assert decision.should_send_offer is False
    assert decision.raw_output["should_send_offer"] is False


@pytest.mark.asyncio
async def test_chat_reply_uses_model_true_offer_decision_after_confirmation() -> None:
    client = FakeOpenRouterClient(
        "Да, жми кнопку ниже. На тест-драйве разберем проект по факту.",
        should_send_offer=True,
    )

    decision = await client.chat_reply("outgoing: Хочешь, отправлю ссылку?", "да")

    assert decision.should_send_offer is True
    assert decision.raw_output["should_send_offer"] is True


@pytest.mark.asyncio
async def test_chat_reply_uses_model_false_offer_decision_for_uncertainty() -> None:
    client = FakeOpenRouterClient(
        "Нормальное сомнение. Что именно неясно в тест-драйве?",
        should_send_offer=False,
    )

    decision = await client.chat_reply("outgoing: Хочешь, скину условия?", "не знаю")

    assert decision.should_send_offer is False
    assert decision.raw_output["should_send_offer"] is False


@pytest.mark.asyncio
async def test_chat_reply_accepts_eval_only_system_prompt_override() -> None:
    client = FakeOpenRouterClient("Что сейчас продаешь?", should_send_offer=False)

    await client.chat_reply("outgoing: В какой нише работаешь?", "фитнес", system_prompt="EVAL PROMPT")

    assert client.last_payload is not None
    assert client.last_payload["messages"][0] == {"role": "system", "content": "EVAL PROMPT"}


@pytest.mark.asyncio
async def test_generate_ping_uses_strict_schema_and_keeps_raw_payloads() -> None:
    response = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {"text": "Вернемся к продукту - какой результат покупает клиент?"}
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
    }
    client = FakePingOpenRouterClient(response)

    result = await client.generate_ping(
        "incoming: продаю консультации\noutgoing: Какой результат покупает клиент?",
        ping_number=2,
        idle_minutes=1440,
    )

    assert result.text == "Вернемся к продукту - какой результат покупает клиент?"
    assert result.response_payload is response
    assert result.usage == response["usage"]
    assert client.last_payload is not None
    assert client.last_payload["response_format"] == {
        "type": "json_schema",
        "json_schema": PING_SCHEMA,
    }
    assert "Номер пинга: 2 из 3" in client.last_payload["messages"][1]["content"]
    assert "1440 минут" in client.last_payload["messages"][1]["content"]


@pytest.mark.asyncio
async def test_generate_ping_rejects_invalid_model_response_with_payloads() -> None:
    response = {"choices": [{"message": {"content": "not-json"}}]}
    client = FakePingOpenRouterClient(response)

    with pytest.raises(OpenRouterError) as captured:
        await client.generate_ping("outgoing: В какой нише проект?", 1, 120)

    assert captured.value.response_payload is response
    assert captured.value.request_payload["response_format"]["json_schema"] == PING_SCHEMA


@pytest.mark.asyncio
@pytest.mark.parametrize("text_value", ["", None, "x" * 4097])
async def test_generate_ping_rejects_empty_or_non_string_text(text_value: object) -> None:
    response = {
        "choices": [{"message": {"content": json.dumps({"text": text_value})}}]
    }
    client = FakePingOpenRouterClient(response)

    with pytest.raises(OpenRouterError, match="Invalid OpenRouter ping response"):
        await client.generate_ping("outgoing: Продолжим?", 1, 120)


@pytest.mark.asyncio
async def test_generate_ping_validates_number_before_api_call() -> None:
    client = FakePingOpenRouterClient({})

    with pytest.raises(ValueError, match="ping_number"):
        await client.generate_ping("", 4, 120)

    assert client.last_payload is None


def test_parse_ping_response_enforces_telegram_length_limit() -> None:
    response = {
        "choices": [{"message": {"content": json.dumps({"text": "x" * 4097})}}]
    }

    with pytest.raises(ValueError, match="4096"):
        parse_ping_response(response)
