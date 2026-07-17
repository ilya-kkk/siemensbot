from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bots import admin_bot


def _message(text: str = "100") -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(username="business_admin"),
        answer=AsyncMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["tech", "business"])
async def test_both_admin_roles_can_begin_growth_alert_setup(monkeypatch, role: str) -> None:
    message = _message("Установить алерт")
    state = SimpleNamespace(set_state=AsyncMock())
    monkeypatch.setattr(admin_bot, "_ensure_admin", AsyncMock(return_value=(1, role)))
    monkeypatch.setattr(
        admin_bot,
        "_growth_alert_recipients",
        AsyncMock(return_value=[{"role": "tech"}, {"role": "business"}]),
    )

    await admin_bot.growth_alert_start(message, state)

    state.set_state.assert_awaited_once_with(admin_bot.AdminStates.waiting_growth_alert_threshold)
    assert "положительное целое число" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_growth_alert_setup_requires_both_admin_chat_ids(monkeypatch) -> None:
    message = _message("Установить алерт")
    state = SimpleNamespace(set_state=AsyncMock())
    monkeypatch.setattr(admin_bot, "_ensure_admin", AsyncMock(return_value=(1, "tech")))
    monkeypatch.setattr(admin_bot, "_growth_alert_recipients", AsyncMock(return_value=[]))

    await admin_bot.growth_alert_start(message, state)

    state.set_state.assert_not_awaited()
    assert "оба" not in message.answer.await_args.args[0].lower()
    assert "технический и бизнес-администратор" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_invalid_growth_alert_value_keeps_fsm_state(monkeypatch) -> None:
    message = _message("1.5")
    state = SimpleNamespace(clear=AsyncMock())
    monkeypatch.setattr(admin_bot, "_ensure_admin", AsyncMock(return_value=(1, "tech")))

    await admin_bot.growth_alert_receive(message, state, SimpleNamespace())

    state.clear.assert_not_awaited()
    assert "положительное целое число" in message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_delivery_marks_success_and_schedules_failed_recipient(monkeypatch) -> None:
    deliveries = [
        {
            "id": 1,
            "chat_id": 101,
            "message_text": "installed",
            "attempt_count": 1,
            "claim_token": "claim-1",
        },
        {
            "id": 2,
            "chat_id": 202,
            "message_text": "installed",
            "attempt_count": 2,
            "claim_token": "claim-2",
        },
    ]
    repo = SimpleNamespace(
        claim_admin_notification_deliveries=AsyncMock(return_value=deliveries),
        complete_admin_notification_delivery=AsyncMock(),
        fail_admin_notification_delivery=AsyncMock(),
    )

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    bot = SimpleNamespace(
        send_message=AsyncMock(side_effect=[None, RuntimeError("blocked")])
    )
    monkeypatch.setattr(admin_bot, "SessionLocal", SessionContext)
    monkeypatch.setattr(admin_bot, "AppRepository", lambda session: repo)

    await admin_bot._deliver_admin_notifications(bot)

    repo.complete_admin_notification_delivery.assert_awaited_once_with(1, "claim-1")
    repo.fail_admin_notification_delivery.assert_awaited_once_with(
        2,
        "claim-2",
        "blocked",
        admin_bot._notification_retry_seconds(2),
    )
