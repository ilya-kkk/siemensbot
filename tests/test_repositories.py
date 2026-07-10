from unittest.mock import AsyncMock

import pytest

from app.repositories import AppRepository


class _ScalarResult:
    def scalar_one(self) -> int:
        return 42


class _OptionalScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value


@pytest.mark.asyncio
async def test_ensure_admin_user_commits() -> None:
    session = AsyncMock()
    session.execute.return_value = _ScalarResult()

    admin_id = await AppRepository(session).ensure_admin_user("@AdminUser", "tech", 123)

    assert admin_id == 42
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_or_create_dialogue_keeps_analyzed_offer_dialogue() -> None:
    session = AsyncMock()
    session.execute.return_value = _OptionalScalarResult(99)

    dialogue_id = await AppRepository(session).get_or_create_dialogue(telegram_user_id=1)

    assert dialogue_id == 99
    query = str(session.execute.call_args.args[0])
    assert "status = 'analyzed' and offer_sent_at is not null" in query
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_dialogue_has_offer_uses_offer_sent_timestamp() -> None:
    session = AsyncMock()
    session.execute.return_value = _OptionalScalarResult(True)

    assert await AppRepository(session).dialogue_has_offer(dialogue_id=99) is True
