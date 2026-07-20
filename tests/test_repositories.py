from datetime import UTC, date, datetime
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


class _OptionalOneRowResult:
    def __init__(self, value: dict[str, object] | None) -> None:
        self.value = value

    def one_or_none(self) -> _MappingRow | None:
        return _MappingRow(self.value) if self.value is not None else None


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
async def test_get_admin_summary_counts_unique_users_in_rolling_24_hours() -> None:
    session = AsyncMock()
    session.execute.return_value = _OneRowResult(
        {"start_24h": 12, "start_all": 148, "lead_24h": 3, "lead_all": 27}
    )

    summary = await AppRepository(session).get_admin_summary()

    assert summary == {
        "start_24h": 12,
        "start_all": 148,
        "lead_24h": 3,
        "lead_all": 27,
    }
    query = str(session.execute.call_args.args[0])
    assert "started_at >= now() - interval '24 hours'" in query
    assert "lead_at >= now() - interval '24 hours'" in query
    assert "from app.telegram_users" in query
    assert "app.messages" not in query
    assert "offer_click_count" not in query


@pytest.mark.asyncio
async def test_set_user_growth_alert_replaces_active_and_enqueues_both_admins() -> None:
    session = AsyncMock()
    session.execute.side_effect = [
        _ScalarResult(None),
        _OptionalScalarResult(9),
        _OneRowResult({"id": 10, "threshold": 100, "set_at": datetime.now(UTC)}),
        _ScalarResult(None),
    ]
    recipients = [
        {"admin_user_id": 1, "role": "tech", "chat_id": 101},
        {"admin_user_id": 2, "role": "business", "chat_id": 202},
    ]

    alert = await AppRepository(session).set_user_growth_alert(
        100, 2, "business_admin", recipients
    )

    assert alert["id"] == 10
    assert alert["replaced"] is True
    replace_query = str(session.execute.call_args_list[1].args[0])
    assert "status = 'replaced'" in replace_query
    delivery_call = session.execute.call_args_list[3]
    assert len(delivery_call.args[1]) == 2
    assert {item["chat_id"] for item in delivery_call.args[1]} == {101, 202}
    assert all("через 100" in item["message_text"] for item in delivery_call.args[1])
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_user_growth_alert_rejects_incomplete_recipient_set() -> None:
    session = AsyncMock()

    with pytest.raises(ValueError):
        await AppRepository(session).set_user_growth_alert(
            100,
            1,
            "tech_admin",
            [{"admin_user_id": 1, "role": "tech", "chat_id": 101}],
        )

    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_trigger_due_user_growth_alert_counts_unique_started_users_and_enqueues_once() -> None:
    session = AsyncMock()
    session.execute.side_effect = [
        _ScalarResult(None),
        _OptionalOneRowResult(
            {"id": 10, "threshold": 100, "reached_count": 101, "triggered_at": datetime.now(UTC)}
        ),
        _ScalarResult(None),
    ]

    alert = await AppRepository(session).trigger_due_user_growth_alert()

    assert alert is not None
    assert alert["reached_count"] == 101
    trigger_query = str(session.execute.call_args_list[1].args[0])
    assert "u.started_at > a.set_at" in trigger_query
    assert "a.status = 'active'" in trigger_query
    assert "status = 'triggered'" in trigger_query
    delivery_query = str(session.execute.call_args_list[2].args[0])
    assert "on conflict" in delivery_query
    assert "event = 'installed'" in delivery_query
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_trigger_due_user_growth_alert_keeps_active_when_threshold_not_reached() -> None:
    session = AsyncMock()
    session.execute.side_effect = [_ScalarResult(None), _OptionalOneRowResult(None)]

    assert await AppRepository(session).trigger_due_user_growth_alert() is None

    assert session.execute.await_count == 2
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_claim_admin_deliveries_uses_lease_and_skip_locked() -> None:
    session = AsyncMock()
    session.execute.return_value = _RowsResult(
        [{"id": 7, "chat_id": 101, "message_text": "alert", "attempt_count": 1}]
    )

    deliveries = await AppRepository(session).claim_admin_notification_deliveries()

    assert len(deliveries) == 1
    assert deliveries[0]["claim_token"] is not None
    query = str(session.execute.call_args.args[0])
    assert "for update skip locked" in query
    assert "claimed_at < now() - make_interval" in query
    assert "attempt_count = attempt_count + 1" in query
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_app_config_seeds_ping_config_once() -> None:
    session = AsyncMock()
    session.execute.side_effect = [
        _OptionalScalarResult(None),
        _OneRowResult(
            {
                "ping_1_delay_minutes": 120,
                "ping_2_delay_minutes": 1440,
                "ping_3_delay_minutes": 4320,
                "updated_at": None,
            }
        ),
    ]

    config = await AppRepository(session).get_app_config()

    assert config["ping_1_delay_minutes"] == 120
    first_query = str(session.execute.call_args_list[0].args[0])
    assert "on conflict (id) do nothing" in first_query
    assert "offer_url" not in first_query
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_referral_source_generates_link_code() -> None:
    session = AsyncMock()
    session.execute.side_effect = [
        _ScalarResult(7),
        _OneRowResult({"id": 7, "source_code": "link7", "title": "Telegram Ads"}),
    ]

    source = await AppRepository(session).create_referral_source(
        "Telegram Ads",
        5,
        "business_admin",
    )

    assert source == {"id": 7, "source_code": "link7", "title": "Telegram Ads"}
    insert_query = str(session.execute.call_args_list[0].args[0])
    insert_params = session.execute.call_args_list[0].args[1]
    update_query = str(session.execute.call_args_list[1].args[0])
    update_params = session.execute.call_args_list[1].args[1]
    assert "insert into app.referral_sources" in insert_query
    assert "returning id" in insert_query
    assert "source_code = 'link' || source.id::text" in update_query
    assert "where source.id = :source_id" in update_query
    assert update_params == {"source_id": 7}
    assert insert_params == {
        "title": "Telegram Ads",
        "created_by_admin_user_id": 5,
        "created_by_username": "business_admin",
    }
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
        referral_source_code="link7",
    )

    assert user_id == 42
    query = str(session.execute.call_args.args[0])
    params = session.execute.call_args.args[1]
    assert "app.dialogues" not in query
    assert "funnel_stage" in query
    assert "dialogue_started_at" in query
    assert "least(app.telegram_users.started_at, excluded.started_at)" in query
    assert "ping_anchor_at = greatest" in query
    assert "ping_message.telegram_message_id < cast(:activity_message_id as bigint)" in query
    assert "ping_message.raw_payload ->> 'ping_number' = '1'" in query
    assert "ping_1_answered_at" in query
    assert "ping_claim_token = null" in query
    assert "app.referral_sources" in query
    assert "referral_source_id = coalesce" in query
    assert params["event_stage"] == "dialogue"
    assert params["activity_at"] == activity_at
    assert params["activity_message_id"] == 101
    assert params["referral_source_code"] == "link7"


@pytest.mark.asyncio
async def test_get_users_by_start_day_returns_only_nonempty_moscow_cohorts() -> None:
    session = AsyncMock()
    session.execute.return_value = _RowsResult(
        [
            {
                "date": date(2026, 7, 18),
                "user_record_id": 10,
                "started_at": datetime(2026, 7, 18, 6, 0, tzinfo=UTC),
                "username": "first",
                "telegram_user_id": 100,
            },
            {
                "date": date(2026, 7, 18),
                "user_record_id": 11,
                "started_at": datetime(2026, 7, 18, 7, 0, tzinfo=UTC),
                "username": None,
                "telegram_user_id": 101,
            },
        ]
    )

    result = await AppRepository(session).get_users_by_start_day(days=2)

    assert result == {
        "daily": [
            {
                "date": date(2026, 7, 18),
                "count": 2,
                "users": [
                    {
                        "started_at": datetime(2026, 7, 18, 6, 0, tzinfo=UTC),
                        "username": "first",
                        "telegram_user_id": 100,
                        "user_record_id": 10,
                    },
                    {
                        "started_at": datetime(2026, 7, 18, 7, 0, tzinfo=UTC),
                        "username": None,
                        "telegram_user_id": 101,
                        "user_record_id": 11,
                    },
                ],
            }
        ]
    }
    query = str(session.execute.call_args.args[0])
    params = session.execute.call_args.args[1]
    assert "generate_series" not in query
    assert "Europe/Moscow" in query
    assert "(u.started_at at time zone 'Europe/Moscow')::date" in query
    assert "u.started_at >=" in query
    assert "u.started_at <" in query
    assert "order by 1 desc, u.started_at, u.id" in query
    assert params == {"days": 2}


@pytest.mark.asyncio
async def test_get_user_messages_by_id_uses_stable_user_record_id() -> None:
    session = AsyncMock()
    session.execute.return_value = _RowsResult(
        [
            {"created_at": "later", "direction": "outgoing", "text": "Ответ"},
            {"created_at": "earlier", "direction": "incoming", "text": "Привет"},
        ]
    )

    rows = await AppRepository(session).get_user_messages_by_id(42)

    assert [row["text"] for row in rows] == ["Привет", "Ответ"]
    query = str(session.execute.call_args.args[0])
    params = session.execute.call_args.args[1]
    assert "where u.id = :user_record_id" in query
    assert params == {"user_record_id": 42, "limit": 80}


@pytest.mark.asyncio
async def test_get_lead_user_id_for_chat_only_matches_leads() -> None:
    session = AsyncMock()
    session.execute.return_value = _OptionalScalarResult(42)

    user_id = await AppRepository(session).get_lead_user_id_for_chat(123)

    assert user_id == 42
    query = str(session.execute.call_args.args[0])
    params = session.execute.call_args.args[1]
    assert "chat_id = :chat_id" in query
    assert "funnel_stage = 'lead'" in query
    assert params == {"chat_id": 123}


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
async def test_analysis_is_refreshed_when_dialogue_changed_or_lead_is_new() -> None:
    session = AsyncMock()
    session.execute.return_value = _OptionalScalarResult(True)

    needed = await AppRepository(session).user_needs_analysis(telegram_user_id=1)

    assert needed is True
    query = str(session.execute.call_args.args[0])
    assert "u.analyzed_at < u.lead_at" in query
    assert "m.created_at > u.analyzed_at" in query


@pytest.mark.asyncio
async def test_offer_click_updates_aggregate_on_user() -> None:
    session = AsyncMock()
    session.execute.return_value = _OptionalScalarResult(1)

    found = await AppRepository(session).record_lead_click(123, 456)

    assert found == 1
    query = str(session.execute.call_args.args[0])
    assert "update app.telegram_users" in query
    assert "offer_click_count = offer_click_count + 1" in query
    assert "chat_id = :chat_id" in query
    assert "telegram_user_id = cast(:telegram_user_id as bigint)" in query
    assert "funnel_stage = 'lead'" in query
    assert "lead_at = coalesce(lead_at, now())" in query
    assert "ping_claim_token = null" in query
    assert "app.link_clicks" not in query
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_lead_export_only_selects_leads_and_analysis_fields() -> None:
    session = AsyncMock()
    session.execute.return_value = _RowsResult([])

    rows = await AppRepository(session).get_leads_for_export()

    assert rows == []
    query = str(session.execute.call_args.args[0])
    assert "where funnel_stage = 'lead'" in query
    for column in (
        "niche",
        "revenue_estimate",
        "average_check",
        "sales_volume",
        "main_problem",
        "lead_temperature",
        "summary",
        "confidence",
    ):
        assert column in query


@pytest.mark.asyncio
async def test_google_sheet_export_only_selects_unsynced_leads() -> None:
    session = AsyncMock()
    session.execute.return_value = _RowsResult([])

    rows = await AppRepository(session).get_unsynced_google_sheet_leads(250)

    assert rows == []
    query = str(session.execute.call_args.args[0])
    assert "funnel_stage = 'lead'" in query
    assert "google_sheet_synced_at is null" in query
    assert session.execute.call_args.args[1] == {"limit": 250}


@pytest.mark.asyncio
async def test_mark_google_sheet_leads_synced_commits() -> None:
    session = AsyncMock()
    session.execute.return_value = _RowsResult([{"id": 1}, {"id": 2}])

    count = await AppRepository(session).mark_google_sheet_leads_synced([1, 2])

    assert count == 2
    query = str(session.execute.call_args.args[0])
    assert "google_sheet_synced_at = coalesce(google_sheet_synced_at, now())" in query
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
                    "dialogue_started": True,
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
    assert "u.dialogue_started_at is not null as dialogue_started" in select_query
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


@pytest.mark.asyncio
async def test_blocked_ping_delivery_marks_funnel_stage_and_clears_retry() -> None:
    session = AsyncMock()
    session.execute.return_value = _OptionalScalarResult(7)

    failed = await AppRepository(session).fail_ping_delivery(
        telegram_user_id=7,
        claim_token="00000000-0000-0000-0000-000000000007",
        user_status="blocked",
    )

    assert failed is True
    query = str(session.execute.call_args.args[0])
    params = session.execute.call_args.args[1]
    assert "then 'blocked'" in query
    assert "stage_updated_at" in query
    assert "cast(:retry_at as timestamptz)" in query
    assert params["user_status"] == "blocked"
    assert params["retry_at"] is None
    session.commit.assert_awaited_once()
