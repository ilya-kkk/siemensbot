import json
from types import SimpleNamespace

import pytest

from app.ai.openrouter import (
    OpenRouterClient,
    _is_invalid_niche_answer,
    _is_off_topic_request,
    _sanitize_reply_text,
)


class FakeOpenRouterClient(OpenRouterClient):
    def __init__(self, reply_text: str, should_send_offer: bool):
        super().__init__(settings=SimpleNamespace(openrouter_model="test-model"))
        self.reply_text = reply_text
        self.model_should_send_offer = should_send_offer

    async def _chat_completion(self, payload: dict) -> dict:
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
async def test_chat_reply_starts_diagnostic_after_plain_followup_affirmation() -> None:
    followup_text = (
        "Привет. После бесплатного обучения лучше не гадать, "
        "а приложить его к твоей ситуации. В какой нише сейчас проект?"
    )
    client = OpenRouterClient(settings=SimpleNamespace(followup_text=followup_text))

    decision = await client.chat_reply(f"outgoing: {followup_text}", "да")

    assert decision.reply_text == "Ок, тогда привяжем обучение к твоей ситуации. В какой нише сейчас проект?"
    assert decision.should_send_offer is False
    assert decision.request_payload["type"] == "local_guard"
    assert decision.request_payload["reason"] == "initial_followup_affirmation"


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
