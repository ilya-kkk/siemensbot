from unittest.mock import AsyncMock

import pytest

from app.repositories import AppRepository


class _ScalarResult:
    def scalar_one(self) -> int:
        return 42


@pytest.mark.asyncio
async def test_ensure_admin_user_commits() -> None:
    session = AsyncMock()
    session.execute.return_value = _ScalarResult()

    admin_id = await AppRepository(session).ensure_admin_user("@AdminUser", "tech", 123)

    assert admin_id == 42
    session.commit.assert_awaited_once()
