from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.repositories import AppRepository


class _ScalarResult:
    def __init__(self, value: object = 42) -> None:
        self.value = value

    def scalar_one(self) -> object:
        return self.value


class _OptionalScalarResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value


class _MappingRow:
    def __init__(self, value: dict[str, object]) -> None:
        self._mapping = value


class _OneRowResult:
    def __init__(self, value: dict[str, object]) -> None:
        self.value = value

    def one(self) -> _MappingRow:
        return _MappingRow(self.value)


class _RowsResult:
    def __init__(self, values: list[dict[str, object]]) -> None:
        self.values = values

    def all(self) -> list[_MappingRow]:
        return [_MappingRow(value) for value in self.values]


class _TranscriptRow:
    def __init__(self, direction: str, text: str) -> None:
        self.direction = direction
        self.text = text


class _TranscriptResult:
    def __init__(self, values: list[_TranscriptRow]) -> None:
        self.values = values

    def all(self) -> list[_TranscriptRow]:
        return self.values


@pytest.mark.asyncio
async def test_ensure_admin_user_commits() -> None:
    session = AsyncMock()
    session.execute.return_value = _ScalarResult()

    admin_id = await AppRepository(session).ensure_admin_user("@AdminUser", "tech", 123)

    assert admin_id == 42
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_app_config_seeds_fallback_url_once() -> None:
    session = AsyncMock()
    session.execute.side_effect = [
        _OptionalScalarResult(None),
        _OptionalScalarResult(None),
        _OneRowResult(
            {
                "offer_url": "https://example.com/form",
                "ping_1_delay_minutes": 120,
                "ping_2_delay_minutes": 1440,
                "ping_3_delay_minutes": 4320,
                "updated_at": None,
            }
        ),
    ]

    config = await AppRepository(session).get_app_config("https://example.com/form")

    assert config["offer_url"] == "https://example.com/form"
    first_query = str(session.execute.call_args_list[0].args[0])
    second_query = str(session.execute.call_args_list[1].args[0])
    assert "on conflict (id) do nothing" in first_query
    assert "offer_url is null" in second_query
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_upsert_user_advances_funnel_without_dialogue_table() -> None:
    session = AsyncMock()
    session.execute.return_value = _ScalarResult()
    activity_at = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)

    user_id = await AppRepository(session).upsert_telegram_user(
        chat_id=123,
        telegram_user_id=456,
        username="client",
        first_name="Анна",
        last_name=None,
        event_stage="dialogue",
        activity_at=activity_at,
        activity_message_id=101,
    )

    assert user_id == 42
    query = str(session.execute.call_args.args[0])
    params = session.execute.call_args.args[1]
    assert "app.dialogues" not in query
    assert "funnel_stage" in query
    assert "dialogue_started_at" in query
    assert "ping_anchor_at = greatest" in query
    assert "ping_message.telegram_message_id < cast(:activity_message_id as bigint)" in query
    assert "ping_message.raw_payload ->> 'ping_number' = '1'" in query
    assert "ping_1_answered_at" in query
    assert "ping_claim_token = null" in query
    assert params["event_stage"] == "dialogue"
    assert params["activity_at"] == activity_at
    assert params["activity_message_id"] == 101


@pytest.mark.asyncio
async def test_log_outgoing_message_links_ai_request() -> None:
    session = AsyncMock()
    session.execute.return_value = _ScalarResult(87)

    message_id = await AppRepository(session).log_message(
        telegram_user_id=1,
        direction="outgoing",
        text_value="Ответ",
        telegram_message_id=999,
        ai_request_id=55,
    )

    assert message_id == 87
    query = str(session.execute.call_args.args[0])
    params = session.execute.call_args.args[1]
    assert "ai_request_id" in query
    assert "dialogue_id" not in query
    assert params["ai_request_id"] == 55
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_transcript_casts_excluded_message_id() -> None:
    session = AsyncMock()
    session.execute.return_value = _TranscriptResult([_TranscriptRow("incoming", "Привет")])

    transcript = await AppRepository(session).get_transcript_for_user(
        telegram_user_id=1,
        exclude_message_id=93,
    )

    query = str(session.execute.call_args.args[0])
    params = session.execute.call_args.args[1]
    assert transcript == "incoming: Привет"
    assert "cast(:exclude_message_id as bigint) is null" in query
    assert "id <> cast(:exclude_message_id as bigint)" in query
    assert params["exclude_message_id"] == 93


@pytest.mark.asyncio
async def test_save_ai_request_keeps_source_and_user_snapshot() -> None:
    session = AsyncMock()
    session.execute.return_value = _ScalarResult(91)

    request_id = await AppRepository(session).save_ai_request(
        telegram_user_id=1,
        source_message_id=80,
        purpose="chat",
        model="test-model",
        status="success",
        request_payload={"messages": [{"role": "user", "content": "Привет"}]},
        response_payload={"choices": []},
        user_snapshot={"id": 1, "funnel_stage": "dialogue"},
        usage={"total_tokens": 10},
    )

    assert request_id == 91
    query = str(session.execute.call_args.args[0])
    params = session.execute.call_args.args[1]
    assert "source_message_id" in query
    assert "user_snapshot" in query
    assert "dialogue_id" not in query
    assert params["source_message_id"] == 80
    assert '"funnel_stage": "dialogue"' in params["user_snapshot"]


@pytest.mark.asyncio
async def test_offer_display_does_not_mark_user_as_lead() -> None:
    session = AsyncMock()

    await AppRepository(session).mark_offer_sent(telegram_user_id=1)

    query = str(session.execute.call_args.args[0])
    assert "update app.telegram_users" in query
    assert "offer_shown_at" in query
    assert "funnel_stage = 'lead'" not in query
    assert "lead_at" not in query
    assert "app.leads" not in query
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_offer_click_updates_aggregate_on_user() -> None:
    session = AsyncMock()
    session.execute.return_value = _OptionalScalarResult(1)

    found = await AppRepository(session).record_offer_click("token")

    assert found is True
    query = str(session.execute.call_args.args[0])
    assert "update app.telegram_users" in query
    assert "offer_click_count = offer_click_count + 1" in query
    assert "offer_legacy_tokens @>" in query
    assert "funnel_stage = 'lead'" in query
    assert "lead_at = coalesce(lead_at, now())" in query
    assert "ping_claim_token = null" in query
    assert "app.link_clicks" not in query
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_stats_returns_overall_and_daily_cohorts() -> None:
    session = AsyncMock()
    overall = {
        "started_users": 10,
        "dialogue_users": 8,
        "in_progress_users": 7,
        "ping_1_sent_users": 6,
        "ping_1_answered_users": 5,
        "ping_2_sent_users": 4,
        "ping_2_answered_users": 3,
        "ping_3_sent_users": 2,
        "ping_3_answered_users": 1,
        "total_leads": 1,
    }
    session.execute.side_effect = [
        _OneRowResult(overall),
        _RowsResult([{"date": "2026-07-13", **overall}]),
        _ScalarResult("0.42"),
    ]

    stats = await AppRepository(session).get_stats()

    assert stats["overall"]["ai_cost_usd"] == "0.42"
    assert stats["daily"][0]["date"] == "2026-07-13"
    assert stats["daily"][0]["metrics"]["ping_3_answered_users"] == 1
    assert "ai_cost_usd" not in stats["daily"][0]["metrics"]
    overall_query = str(session.execute.call_args_list[0].args[0])
    daily_query = str(session.execute.call_args_list[1].args[0])
    assert "ping_3_answered_at" in overall_query
    assert "app.messages" in overall_query
    assert "^/start" in overall_query
    assert "Europe/Moscow" in daily_query
    assert "(m.created_at at time zone 'Europe/Moscow')::date" in daily_query
    assert "^/start" in daily_query
    assert "- 13" in daily_query


@pytest.mark.asyncio
async def test_claim_due_ping_uses_absolute_anchor_and_lease() -> None:
    session = AsyncMock()
    session.execute.side_effect = [
        _RowsResult(
            [
                {
                    "telegram_user_id": 7,
                    "chat_id": 70,
                    "ping_number": 2,
                    "anchor_at": None,
                    "idle_minutes": 1440,
                    "offer_shown": True,
                    "ping_pending_ai_request_id": None,
                    "pending_response_payload": None,
                    "offer_url": "https://example.com",
                    "ping_1_delay_minutes": 120,
                    "ping_2_delay_minutes": 1440,
                    "ping_3_delay_minutes": 4320,
                }
            ]
        ),
        _OptionalScalarResult(7),
    ]

    claims = await AppRepository(session).claim_due_ping_users(limit=1, lease_seconds=600)

    assert len(claims) == 1
    assert claims[0]["ping_number"] == 2
    assert claims[0]["claim_token"] is not None
    select_query = str(session.execute.call_args_list[0].args[0])
    assert "u.ping_anchor_at + make_interval" in select_query
    assert "when 0 then c.ping_1_delay_minutes" in select_query
    assert "ping_claimed_at < now() - make_interval" in select_query
    assert "u.pings_sent_count < 3" in select_query
    assert "u.funnel_stage <> 'lead'" in select_query
    assert "for update of u skip locked" in select_query
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_complete_ping_send_updates_counter_and_links_ai_request() -> None:
    session = AsyncMock()
    session.execute.side_effect = [_OptionalScalarResult(7), _OptionalScalarResult(None)]
    sent_at = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)

    completed = await AppRepository(session).complete_ping_send(
        telegram_user_id=7,
        claim_token="00000000-0000-0000-0000-000000000007",
        ping_number=2,
        ai_request_id=90,
        text_value="Продолжим разбор?",
        telegram_message_id=123,
        raw_payload={"event": "ping", "ping_number": 2},
        sent_at=sent_at,
    )

    assert completed is True
    update_query = str(session.execute.call_args_list[0].args[0])
    insert_query = str(session.execute.call_args_list[1].args[0])
    update_params = session.execute.call_args_list[0].args[1]
    insert_params = session.execute.call_args_list[1].args[1]
    assert "ping_2_sent_at" in update_query
    assert "coalesce" in update_query
    assert "ping_pending_ai_request_id = :ai_request_id" in update_query
    assert update_params["ai_request_id"] == 90
    assert update_params["sent_at"] == sent_at
    assert "insert into app.messages" in insert_query
    assert insert_params["ai_request_id"] == 90
    session.commit.assert_awaited_once()
