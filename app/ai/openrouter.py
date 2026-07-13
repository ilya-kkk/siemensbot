import asyncio
import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.core.config import Settings

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
DASH_TRANSLATION = str.maketrans({"—": "-", "–": "-"})
LEADING_ACK_RE = re.compile(
    r"^\s*(понятно|понял|поняла|окей|ок|да|хорошо|принял|приняла|зафиксировал|зафиксировала)\b[,.!]*\s*",
    re.IGNORECASE,
)
TECH_TOPIC_RE = re.compile(
    r"\b(python|питон|javascript|js|typescript|sql|html|css|api|docker|git)\b"
    r"|код|скрипт|функци|алгоритм|сортировк|пузырьк|регулярк|программ",
    re.IGNORECASE,
)
TASK_ACTION_RE = re.compile(
    r"\b(напиши|написать|сделай|создай|реализуй|покажи|объясни|реши|сгенерируй|дай|скинь)\b",
    re.IGNORECASE,
)
GENERAL_OFF_TOPIC_RE = re.compile(
    r"\b(переведи|перевести|сочини|анекдот|рецепт|погода|курс валют|новости|кто такой|что такое)\b",
    re.IGNORECASE,
)
CONTENT_TASK_RE = re.compile(
    r"\b(текст|пост|статью|эссе|письмо|объявление|резюме|сценарий|презентацию)\b",
    re.IGNORECASE,
)
DOMAIN_TOPIC_RE = re.compile(
    r"тест-драйв|наставнич|разбор|проект|воронк|продукт|ниша|заявк|клиент|продаж|чек|оплат|аудитор|лид|упаковк|оффер|эксперт|инфобиз|бот",
    re.IGNORECASE,
)
INVALID_NICHE_ANSWER_RE = re.compile(
    r"^\s*(хз|не знаю|без понятия|не понимаю|не понял|не поняла|не скажу|любая|любая ниша|что угодно|неважно|пофиг|все равно|тест|test|проверка)\s*[.!?]*\s*$",
    re.IGNORECASE,
)
KEYBOARD_MASH_RE = re.compile(
    r"asdf|qwer|zxcv|йцук|фыв|ыва|вап|олд|джэ|ячс",
    re.IGNORECASE,
)
DIAGNOSTIC_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("в какой нише", "В какой нише сейчас проект?"),
    ("что сейчас прода", "Что сейчас продаешь?"),
    ("мешает зарабатывать", "Что сейчас сильнее всего мешает зарабатывать больше?"),
    ("мешает масштаб", "Что сейчас сильнее всего мешает масштабироваться?"),
    ("средний чек", "Какой средний чек?"),
    ("сколько оплат", "Сколько оплат в среднем приходит за месяц?"),
    ("сколько клиентов", "Сколько оплат в среднем приходит за месяц?"),
    ("откуда сейчас приход", "Откуда сейчас приходят заявки?"),
    ("главный затык", "Что сейчас сильнее всего мешает зарабатывать больше?"),
    ("где сам чувствуешь", "Что сейчас сильнее всего мешает зарабатывать больше?"),
    ("понятный путь", "Есть ли понятный путь человека от первого касания до покупки?"),
    ("путь человека", "Есть ли понятный путь человека от первого касания до покупки?"),
)


class OpenRouterError(RuntimeError):
    def __init__(
        self,
        message: str,
        request_payload: dict[str, Any] | None = None,
        response_payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.request_payload = request_payload or {}
        self.response_payload = response_payload or {}


@dataclass(frozen=True)
class ChatDecision:
    reply_text: str
    should_send_offer: bool
    raw_output: dict[str, Any]
    usage: dict[str, Any] | None
    request_payload: dict[str, Any]
    response_payload: dict[str, Any]


@dataclass(frozen=True)
class AnalysisResult:
    output: dict[str, Any]
    usage: dict[str, Any] | None
    request_payload: dict[str, Any]
    response_payload: dict[str, Any]


@dataclass(frozen=True)
class PingResult:
    text: str
    usage: dict[str, Any] | None
    request_payload: dict[str, Any]
    response_payload: dict[str, Any]


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def load_json_schema(name: str) -> dict[str, Any]:
    return json.loads((PROMPTS_DIR / name).read_text(encoding="utf-8"))


def _capitalize_first(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _recent_outgoing_started_with_ack(transcript: str, limit: int = 2) -> bool:
    seen = 0
    for line in reversed(transcript.splitlines()):
        if not line.startswith("outgoing:"):
            continue
        seen += 1
        message = line.removeprefix("outgoing:").strip()
        if LEADING_ACK_RE.match(message):
            return True
        if seen >= limit:
            return False
    return False


def _sanitize_reply_text(reply_text: str, transcript: str = "") -> str:
    text = reply_text.translate(DASH_TRANSLATION).strip()
    if LEADING_ACK_RE.match(text) and _recent_outgoing_started_with_ack(transcript):
        text = _capitalize_first(LEADING_ACK_RE.sub("", text, count=1).lstrip())
    return text


def _is_off_topic_request(user_message: str) -> bool:
    text = user_message.strip().lower()
    if not text:
        return False
    if GENERAL_OFF_TOPIC_RE.search(text) and not DOMAIN_TOPIC_RE.search(text):
        return True
    if TASK_ACTION_RE.search(text) and CONTENT_TASK_RE.search(text):
        return True
    return bool(TECH_TOPIC_RE.search(text) and (TASK_ACTION_RE.search(text) or "сортировк" in text))


def _last_diagnostic_question(transcript: str) -> str:
    for line in reversed(transcript.splitlines()):
        if not line.startswith("outgoing:"):
            continue
        message = line.removeprefix("outgoing:").strip().lower()
        for marker, question in DIAGNOSTIC_QUESTIONS:
            if marker in message:
                return question
    return "В какой нише сейчас проект?"


def _off_topic_redirect_reply(transcript: str) -> str:
    question = _last_diagnostic_question(transcript)
    return f"Давай вернемся к разбору проекта. {question}"


def _last_outgoing_message(transcript: str) -> str:
    for line in reversed(transcript.splitlines()):
        if line.startswith("outgoing:"):
            return line.removeprefix("outgoing:").strip()
    return ""


def _last_outgoing_asks_for_niche(transcript: str) -> bool:
    message = _last_outgoing_message(transcript).lower()
    return any(
        marker in message
        for marker in (
            "в какой нише",
            "в какой реальной нише",
            "это не ниша",
            "формулировку ниши",
            "чем занимаешься или с кем работаешь",
        )
    )


def _is_invalid_niche_answer(user_message: str) -> bool:
    text = user_message.strip().lower()
    if not text:
        return True
    if INVALID_NICHE_ANSWER_RE.match(text):
        return True

    compact = re.sub(r"[^a-zа-яё0-9]+", "", text)
    letters = re.sub(r"[^a-zа-яё]+", "", text)
    if not letters:
        return True
    if len(compact) >= 4 and len(set(compact)) == 1:
        return True
    if 4 <= len(compact) <= 16 and KEYBOARD_MASH_RE.search(compact):
        return True
    if re.fullmatch(r"[a-z]{5,}", letters) and not re.search(r"[aeiouy]", letters):
        return True
    return bool(re.fullmatch(r"[а-яё]{5,}", letters) and not re.search(r"[аеёиоуыэюя]", letters))


def _invalid_niche_reply(transcript: str) -> str:
    return _sanitize_reply_text(
        "Давай проще: напиши хотя бы примерно, чем занимаешься или с кем работаешь?",
        transcript,
    )


def parse_ping_response(response: Mapping[str, Any], transcript: str = "") -> str:
    content = response["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("text"), str):
        raise ValueError("ping text must be a string")
    text = _sanitize_reply_text(parsed["text"], transcript)
    if not text:
        raise ValueError("ping text is empty")
    if len(text) > 4096:
        raise ValueError("ping text exceeds Telegram's 4096-character limit")
    return text


CHAT_SCHEMA: dict[str, Any] = {
    "name": "telegram_chat_reply",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["reply_text", "should_send_offer"],
        "properties": {
            "reply_text": {"type": "string"},
            "should_send_offer": {"type": "boolean"},
        },
    },
}

PING_SCHEMA: dict[str, Any] = {
    "name": "telegram_ping_reply",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["text"],
        "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": 4096},
        },
    },
}


class OpenRouterClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def _chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.openrouter_api_key:
            raise OpenRouterError("OPENROUTER_API_KEY is not configured", request_payload=payload)

        headers = {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.settings.public_base_url or "http://localhost:8000",
            "X-Title": "Siemensbot",
        }
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise OpenRouterError(str(exc), request_payload=payload) from exc
        if response.status_code >= 400:
            raise OpenRouterError(
                f"OpenRouter error {response.status_code}: {response.text[:500]}",
                request_payload=payload,
                response_payload={"status_code": response.status_code, "body": response.text},
            )
        try:
            return response.json()
        except ValueError as exc:
            raise OpenRouterError(
                f"Invalid OpenRouter JSON response: {exc}",
                request_payload=payload,
                response_payload={"status_code": response.status_code, "body": response.text},
            ) from exc

    async def transcribe_ogg(self, audio_bytes: bytes) -> str:
        payload = {
            "model": self.settings.openrouter_stt_model,
            "input_audio": {
                "data": base64.b64encode(audio_bytes).decode("ascii"),
                "format": "ogg",
            },
            "language": "ru",
        }
        data = await self._post_transcription(payload)
        text = data.get("text")
        if not isinstance(text, str):
            raise OpenRouterError(
                "Invalid OpenRouter transcription response: missing text",
                request_payload=payload,
                response_payload=data,
            )
        return text.strip()

    async def _post_transcription(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.openrouter_api_key:
            raise OpenRouterError("OPENROUTER_API_KEY is not configured", request_payload=payload)

        headers = {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.settings.public_base_url or "http://localhost:8000",
            "X-Title": "Siemensbot",
        }
        url = "https://openrouter.ai/api/v1/audio/transcriptions"
        timeout = httpx.Timeout(45)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(3):
                try:
                    response = await client.post(url, headers=headers, json=payload)
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (2**attempt))
                        continue
                    raise OpenRouterError(str(exc), request_payload=payload) from exc

                if (response.status_code == 429 or response.status_code >= 500) and attempt < 2:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                if response.status_code >= 400:
                    raise OpenRouterError(
                        f"OpenRouter error {response.status_code}: {response.text[:500]}",
                        request_payload=payload,
                        response_payload={
                            "status_code": response.status_code,
                            "body": response.text,
                        },
                    )
                try:
                    data = response.json()
                except ValueError as exc:
                    raise OpenRouterError(
                        f"Invalid OpenRouter transcription JSON response: {exc}",
                        request_payload=payload,
                        response_payload={
                            "status_code": response.status_code,
                            "body": response.text,
                        },
                    ) from exc
                if not isinstance(data, dict):
                    raise OpenRouterError(
                        "Invalid OpenRouter transcription JSON response",
                        request_payload=payload,
                        response_payload={"body": data},
                    )
                return data

        raise OpenRouterError("OpenRouter transcription failed", request_payload=payload)

    async def chat_reply(
        self,
        transcript: str,
        user_message: str,
        *,
        system_prompt: str | None = None,
    ) -> ChatDecision:
        if _is_off_topic_request(user_message):
            reply_text = _sanitize_reply_text(_off_topic_redirect_reply(transcript), transcript)
            parsed = {"reply_text": reply_text, "should_send_offer": False}
            local_payload = {
                "type": "local_guard",
                "reason": "off_topic_redirect",
                "user_message": user_message,
            }
            return ChatDecision(
                reply_text=reply_text,
                should_send_offer=False,
                raw_output=parsed,
                usage=None,
                request_payload=local_payload,
                response_payload=parsed,
            )

        if _last_outgoing_asks_for_niche(transcript) and _is_invalid_niche_answer(user_message):
            reply_text = _invalid_niche_reply(transcript)
            parsed = {"reply_text": reply_text, "should_send_offer": False}
            local_payload = {
                "type": "local_guard",
                "reason": "invalid_niche_answer",
                "user_message": user_message,
            }
            return ChatDecision(
                reply_text=reply_text,
                should_send_offer=False,
                raw_output=parsed,
                usage=None,
                request_payload=local_payload,
                response_payload=parsed,
            )

        if system_prompt is None:
            system_prompt = load_prompt("user_chat.system.md")
        payload = {
            "model": self.settings.openrouter_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Диалог:\n{transcript}\n\nНовое сообщение:\n{user_message}"},
            ],
            "response_format": {"type": "json_schema", "json_schema": CHAT_SCHEMA},
        }
        response = await self._chat_completion(payload)
        try:
            content = response["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise OpenRouterError(
                f"Invalid OpenRouter chat response: {exc}",
                request_payload=payload,
                response_payload=response,
            ) from exc
        reply_text = _sanitize_reply_text(str(parsed["reply_text"]), transcript)
        parsed["reply_text"] = reply_text
        should_send_offer = bool(parsed["should_send_offer"])
        return ChatDecision(
            reply_text=reply_text,
            should_send_offer=should_send_offer,
            raw_output=parsed,
            usage=response.get("usage"),
            request_payload=payload,
            response_payload=response,
        )

    async def analyze_dialogue(self, transcript: str) -> AnalysisResult:
        system_prompt = load_prompt("dialog_analysis.system.md")
        schema = load_json_schema("dialog_analysis.schema.json")
        payload = {
            "model": self.settings.openrouter_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": transcript},
            ],
            "response_format": {"type": "json_schema", "json_schema": schema},
        }
        response = await self._chat_completion(payload)
        try:
            content = response["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise OpenRouterError(
                f"Invalid OpenRouter analysis response: {exc}",
                request_payload=payload,
                response_payload=response,
            ) from exc
        return AnalysisResult(
            output=parsed,
            usage=response.get("usage"),
            request_payload=payload,
            response_payload=response,
        )

    async def generate_ping(
        self,
        transcript: str,
        ping_number: int,
        idle_minutes: int,
    ) -> PingResult:
        if ping_number not in (1, 2, 3):
            raise ValueError("ping_number must be between 1 and 3")
        if idle_minutes < 0:
            raise ValueError("idle_minutes must be non-negative")

        system_prompt = load_prompt("ping.system.md")
        payload = {
            "model": self.settings.openrouter_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        f"Номер пинга: {ping_number} из 3.\n"
                        f"Пользователь не отвечает уже {idle_minutes} минут.\n\n"
                        f"Контекст диалога:\n{transcript}"
                    ),
                },
            ],
            "response_format": {"type": "json_schema", "json_schema": PING_SCHEMA},
        }
        response = await self._chat_completion(payload)
        try:
            text = parse_ping_response(response, transcript)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise OpenRouterError(
                f"Invalid OpenRouter ping response: {exc}",
                request_payload=payload,
                response_payload=response,
            ) from exc
        return PingResult(
            text=text,
            usage=response.get("usage"),
            request_payload=payload,
            response_payload=response,
        )
