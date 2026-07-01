import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.core.config import Settings

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"


class OpenRouterError(RuntimeError):
    pass


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


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def load_json_schema(name: str) -> dict[str, Any]:
    return json.loads((PROMPTS_DIR / name).read_text(encoding="utf-8"))


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


class OpenRouterClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def _chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.openrouter_api_key:
            raise OpenRouterError("OPENROUTER_API_KEY is not configured")

        headers = {
            "Authorization": f"Bearer {self.settings.openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.settings.public_base_url or "http://localhost:8000",
            "X-Title": "Siemensbot",
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
            )
        if response.status_code >= 400:
            raise OpenRouterError(f"OpenRouter error {response.status_code}: {response.text[:500]}")
        return response.json()

    async def chat_reply(self, transcript: str, user_message: str) -> ChatDecision:
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
        content = response["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return ChatDecision(
            reply_text=str(parsed["reply_text"]),
            should_send_offer=bool(parsed["should_send_offer"]),
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
        content = response["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return AnalysisResult(
            output=parsed,
            usage=response.get("usage"),
            request_payload=payload,
            response_payload=response,
        )
