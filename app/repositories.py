import json
import socket
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.tokens import make_link_token


def _normalize_username(username: str | None) -> str | None:
    if not username:
        return None
    return username.strip().lstrip("@").lower() or None


class AppRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def ensure_admin_user(self, username: str, role: str, chat_id: int | None = None) -> int:
        normalized = _normalize_username(username)
        result = await self.session.execute(
            text(
                """
                insert into app.admin_users (username, username_normalized, role, chat_id, first_seen_at, last_seen_at)
                values (:username, :normalized, :role, :chat_id, now(), now())
                on conflict (username_normalized) do update set
                  chat_id = coalesce(excluded.chat_id, app.admin_users.chat_id),
                  role = excluded.role,
                  is_active = true,
                  last_seen_at = now(),
                  updated_at = now()
                returning id
                """
            ),
            {
                "username": username.lstrip("@"),
                "normalized": normalized,
                "role": role,
                "chat_id": chat_id,
            },
        )
        admin_id = int(result.scalar_one())
        await self.session.commit()
        return admin_id

    async def get_tech_admin_chat_id(self, username_normalized: str | None = None) -> int | None:
        result = await self.session.execute(
            text(
                """
                select chat_id
                from app.admin_users
                where role = 'tech' and is_active = true and chat_id is not null
                  and (
                    cast(:username_normalized as text) is null
                    or username_normalized = cast(:username_normalized as text)
                  )
                order by last_seen_at desc nulls last
                limit 1
                """
            ),
            {"username_normalized": username_normalized},
        )
        value = result.scalar_one_or_none()
        return int(value) if value is not None else None

    async def upsert_service_heartbeat(
        self,
        component: str,
        status: str = "ok",
        details: dict[str, Any] | None = None,
    ) -> None:
        await self.session.execute(
            text(
                """
                insert into app.service_heartbeats (
                  component, instance_id, status, details, updated_at
                )
                values (
                  :component, :instance_id, :status, cast(:details as jsonb), now()
                )
                on conflict (component) do update set
                  instance_id = excluded.instance_id,
                  status = excluded.status,
                  details = excluded.details,
                  updated_at = now()
                """
            ),
            {
                "component": component,
                "instance_id": socket.gethostname(),
                "status": status,
                "details": json.dumps(details or {}),
            },
        )
        await self.session.commit()

    async def get_service_health(
        self,
        components: tuple[str, ...],
        stale_seconds: int,
    ) -> dict[str, dict[str, Any]]:
        result = await self.session.execute(
            text(
                """
                select component, status, updated_at
                from app.service_heartbeats
                where component = any(cast(:components as text[]))
                """
            ),
            {"components": list(components)},
        )
        rows = {str(row.component): row for row in result.all()}
        cutoff = datetime.now(UTC) - timedelta(seconds=max(1, stale_seconds))
        health: dict[str, dict[str, Any]] = {}
        for component in components:
            row = rows.get(component)
            updated_at = row.updated_at if row is not None else None
            healthy = bool(
                row is not None
                and row.status == "ok"
                and updated_at is not None
                and updated_at >= cutoff
            )
            health[component] = {
                "status": "ok" if healthy else "stale",
                "updated_at": updated_at.isoformat() if updated_at is not None else None,
            }
        return health

    async def get_app_config(self, default_offer_url: str) -> dict[str, Any]:
        """Return the singleton runtime config, seeding its URL exactly once."""
        await self.session.execute(
            text(
                """
                insert into app.config (id, offer_url)
                values (1, nullif(btrim(:default_offer_url), ''))
                on conflict (id) do nothing
                """
            ),
            {"default_offer_url": default_offer_url},
        )
        await self.session.execute(
            text(
                """
                update app.config
                set offer_url = nullif(btrim(:default_offer_url), ''), updated_at = now()
                where id = 1
                  and offer_url is null
                  and nullif(btrim(:default_offer_url), '') is not null
                """
            ),
            {"default_offer_url": default_offer_url},
        )
        result = await self.session.execute(
            text(
                """
                select
                  offer_url,
                  ping_1_delay_minutes,
                  ping_2_delay_minutes,
                  ping_3_delay_minutes,
                  updated_at
                from app.config
                where id = 1
                """
            )
        )
        row = result.one()
        await self.session.commit()
        return dict(row._mapping)

    async def set_offer_url(self, offer_url: str) -> None:
        await self.session.execute(
            text(
                """
                update app.config
                set offer_url = :offer_url, updated_at = now()
                where id = 1
                """
            ),
            {"offer_url": offer_url},
        )
        await self.session.commit()

    async def set_ping_delays(self, delays_minutes: tuple[int, int, int]) -> None:
        ping_1, ping_2, ping_3 = delays_minutes
        await self.session.execute(
            text(
                """
                update app.config
                set ping_1_delay_minutes = :ping_1,
                    ping_2_delay_minutes = :ping_2,
                    ping_3_delay_minutes = :ping_3,
                    updated_at = now()
                where id = 1
                """
            ),
            {"ping_1": ping_1, "ping_2": ping_2, "ping_3": ping_3},
        )
        await self.session.commit()

    async def upsert_telegram_user(
        self,
        chat_id: int,
        telegram_user_id: int | None,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        event_stage: str,
        activity_at: datetime | None = None,
        activity_message_id: int | None = None,
    ) -> int:
        result = await self.session.execute(
            text(
                """
                insert into app.telegram_users (
                  chat_id, telegram_user_id, username, username_normalized, first_name, last_name,
                  status, funnel_stage, stage_updated_at, started_at, dialogue_started_at,
                  first_seen_at, last_seen_at, ping_anchor_at
                )
                values (
                  :chat_id, :telegram_user_id, :username, :username_normalized, :first_name, :last_name,
                  'active', :event_stage, coalesce(:activity_at, now()),
                  case when :event_stage = 'started' then coalesce(:activity_at, now()) end,
                  case when :event_stage = 'dialogue' then coalesce(:activity_at, now()) end,
                  coalesce(:activity_at, now()), coalesce(:activity_at, now()),
                  coalesce(:activity_at, now())
                )
                on conflict (chat_id) do update set
                  telegram_user_id = coalesce(excluded.telegram_user_id, app.telegram_users.telegram_user_id),
                  username = coalesce(excluded.username, app.telegram_users.username),
                  username_normalized = coalesce(excluded.username_normalized, app.telegram_users.username_normalized),
                  first_name = coalesce(excluded.first_name, app.telegram_users.first_name),
                  last_name = coalesce(excluded.last_name, app.telegram_users.last_name),
                  status = case
                    when app.telegram_users.status in ('blocked', 'invalid') then app.telegram_users.status
                    else 'active'
                  end,
                  funnel_stage = case
                    when app.telegram_users.funnel_stage = 'lead' then 'lead'
                    when excluded.funnel_stage = 'dialogue' then 'dialogue'
                    else app.telegram_users.funnel_stage
                  end,
                  stage_updated_at = case
                    when app.telegram_users.funnel_stage = 'lead' then app.telegram_users.stage_updated_at
                    when excluded.funnel_stage = 'dialogue'
                      and app.telegram_users.funnel_stage <> 'dialogue'
                      then coalesce(:activity_at, now())
                    else app.telegram_users.stage_updated_at
                  end,
                  started_at = case
                    when app.telegram_users.started_at is null then excluded.started_at
                    when excluded.started_at is null then app.telegram_users.started_at
                    else least(app.telegram_users.started_at, excluded.started_at)
                  end,
                  dialogue_started_at = case
                    when app.telegram_users.dialogue_started_at is null
                      then excluded.dialogue_started_at
                    when excluded.dialogue_started_at is null
                      then app.telegram_users.dialogue_started_at
                    else least(
                      app.telegram_users.dialogue_started_at,
                      excluded.dialogue_started_at
                    )
                  end,
                  first_seen_at = least(
                    app.telegram_users.first_seen_at,
                    excluded.first_seen_at
                  ),
                  last_seen_at = greatest(
                    coalesce(app.telegram_users.last_seen_at, coalesce(:activity_at, now())),
                    coalesce(:activity_at, now())
                  ),
                  ping_anchor_at = greatest(
                    coalesce(app.telegram_users.ping_anchor_at, coalesce(:activity_at, now())),
                    coalesce(:activity_at, now())
                  ),
                  ping_1_answered_at = case
                    when app.telegram_users.pings_sent_count = 1
                      and app.telegram_users.ping_1_sent_at is not null
                      and app.telegram_users.ping_1_answered_at is null
                      and (
                        (
                          cast(:activity_message_id as bigint) is not null
                          and exists (
                            select 1
                            from app.messages ping_message
                            where ping_message.telegram_user_id = app.telegram_users.id
                              and ping_message.direction = 'outgoing'
                              and ping_message.telegram_message_id < cast(:activity_message_id as bigint)
                              and ping_message.raw_payload ->> 'event' = 'ping'
                              and ping_message.raw_payload ->> 'ping_number' = '1'
                          )
                        )
                        or (
                          cast(:activity_message_id as bigint) is null
                          and app.telegram_users.ping_1_sent_at <= coalesce(:activity_at, now())
                        )
                      )
                      then coalesce(:activity_at, now())
                    else app.telegram_users.ping_1_answered_at
                  end,
                  ping_2_answered_at = case
                    when app.telegram_users.pings_sent_count = 2
                      and app.telegram_users.ping_2_sent_at is not null
                      and app.telegram_users.ping_2_answered_at is null
                      and (
                        (
                          cast(:activity_message_id as bigint) is not null
                          and exists (
                            select 1
                            from app.messages ping_message
                            where ping_message.telegram_user_id = app.telegram_users.id
                              and ping_message.direction = 'outgoing'
                              and ping_message.telegram_message_id < cast(:activity_message_id as bigint)
                              and ping_message.raw_payload ->> 'event' = 'ping'
                              and ping_message.raw_payload ->> 'ping_number' = '2'
                          )
                        )
                        or (
                          cast(:activity_message_id as bigint) is null
                          and app.telegram_users.ping_2_sent_at <= coalesce(:activity_at, now())
                        )
                      )
                      then coalesce(:activity_at, now())
                    else app.telegram_users.ping_2_answered_at
                  end,
                  ping_3_answered_at = case
                    when app.telegram_users.pings_sent_count = 3
                      and app.telegram_users.ping_3_sent_at is not null
                      and app.telegram_users.ping_3_answered_at is null
                      and (
                        (
                          cast(:activity_message_id as bigint) is not null
                          and exists (
                            select 1
                            from app.messages ping_message
                            where ping_message.telegram_user_id = app.telegram_users.id
                              and ping_message.direction = 'outgoing'
                              and ping_message.telegram_message_id < cast(:activity_message_id as bigint)
                              and ping_message.raw_payload ->> 'event' = 'ping'
                              and ping_message.raw_payload ->> 'ping_number' = '3'
                          )
                        )
                        or (
                          cast(:activity_message_id as bigint) is null
                          and app.telegram_users.ping_3_sent_at <= coalesce(:activity_at, now())
                        )
                      )
                      then coalesce(:activity_at, now())
                    else app.telegram_users.ping_3_answered_at
                  end,
                  ping_claim_token = null,
                  ping_claim_number = null,
                  ping_claimed_at = null,
                  ping_retry_at = null,
                  ping_pending_ai_request_id = null,
                  updated_at = now()
                returning id
                """
            ),
            {
                "chat_id": chat_id,
                "telegram_user_id": telegram_user_id,
                "username": username,
                "username_normalized": _normalize_username(username),
                "first_name": first_name,
                "last_name": last_name,
                "event_stage": event_stage,
                "activity_at": activity_at,
                "activity_message_id": activity_message_id,
            },
        )
        return int(result.scalar_one())

    async def get_user_snapshot(self, telegram_user_id: int) -> dict[str, Any]:
        result = await self.session.execute(
            text(
                """
                select jsonb_build_object(
                  'id', id,
                  'chat_id', chat_id,
                  'telegram_user_id', telegram_user_id,
                  'username', username,
                  'first_name', first_name,
                  'last_name', last_name,
                  'status', status,
                  'funnel_stage', funnel_stage,
                  'started_at', started_at,
                  'dialogue_started_at', dialogue_started_at,
                  'offer_shown_at', offer_shown_at,
                  'lead_at', lead_at,
                  'pings_sent_count', pings_sent_count,
                  'ping_anchor_at', ping_anchor_at
                )
                from app.telegram_users
                where id = :telegram_user_id
                """
            ),
            {"telegram_user_id": telegram_user_id},
        )
        return dict(result.scalar_one())

    async def log_message(
        self,
        telegram_user_id: int,
        direction: str,
        text_value: str | None,
        telegram_message_id: int | None,
        raw_payload: dict[str, Any] | None = None,
        message_type: str = "text",
        ai_request_id: int | None = None,
    ) -> int:
        result = await self.session.execute(
            text(
                """
                insert into app.messages (
                  telegram_user_id, ai_request_id, direction, message_type, text,
                  telegram_message_id, raw_payload
                )
                values (
                  :telegram_user_id, :ai_request_id, :direction, :message_type, :text,
                  :telegram_message_id, cast(:raw_payload as jsonb)
                )
                returning id
                """
            ),
            {
                "telegram_user_id": telegram_user_id,
                "ai_request_id": ai_request_id,
                "direction": direction,
                "message_type": message_type,
                "text": text_value,
                "telegram_message_id": telegram_message_id,
                "raw_payload": json.dumps(raw_payload or {}),
            },
        )
        await self.session.commit()
        return int(result.scalar_one())

    async def get_user_messages(self, query: str, limit: int = 80) -> list[dict[str, Any]]:
        params: dict[str, Any]
        if query.lstrip("-").isdigit():
            where = "u.chat_id = :chat_id"
            params = {"chat_id": int(query)}
        else:
            where = "u.username_normalized = :username"
            params = {"username": _normalize_username(query)}
        result = await self.session.execute(
            text(
                f"""
                select
                  m.created_at,
                  m.direction,
                  m.text,
                  u.username,
                  nullif(btrim(concat_ws(' ', u.first_name, u.last_name)), '') as telegram_name
                from app.messages m
                join app.telegram_users u on u.id = m.telegram_user_id
                where {where}
                order by m.created_at desc, m.id desc
                limit :limit
                """
            ),
            {**params, "limit": limit},
        )
        rows = [dict(row._mapping) for row in result.all()]
        rows.reverse()
        return rows

    async def get_transcript_for_user(
        self,
        telegram_user_id: int,
        exclude_message_id: int | None = None,
    ) -> str:
        result = await self.session.execute(
            text(
                """
                select direction, text
                from app.messages
                where telegram_user_id = :telegram_user_id
                  and text is not null
                  and (
                    cast(:exclude_message_id as bigint) is null
                    or id <> cast(:exclude_message_id as bigint)
                  )
                order by created_at, id
                """
            ),
            {
                "telegram_user_id": telegram_user_id,
                "exclude_message_id": exclude_message_id,
            },
        )
        return "\n".join(f"{row.direction}: {row.text}" for row in result.all())

    async def save_ai_request(
        self,
        telegram_user_id: int,
        source_message_id: int,
        purpose: str,
        model: str,
        status: str,
        request_payload: dict[str, Any],
        response_payload: dict[str, Any],
        user_snapshot: dict[str, Any],
        usage: dict[str, Any] | None,
        error_message: str | None = None,
        provider: str = "openrouter",
        *,
        commit: bool = True,
    ) -> int:
        usage = usage or {}
        result = await self.session.execute(
            text(
                """
                insert into app.ai_requests (
                  telegram_user_id, source_message_id, provider, model, purpose, status,
                  request_payload, response_payload, user_snapshot,
                  prompt_tokens, completion_tokens, total_tokens, usage_cost, error_message
                )
                values (
                  :telegram_user_id, :source_message_id, :provider, :model, :purpose, :status,
                  cast(:request_payload as jsonb), cast(:response_payload as jsonb),
                  cast(:user_snapshot as jsonb),
                  :prompt_tokens, :completion_tokens, :total_tokens, :usage_cost, :error_message
                )
                returning id
                """
            ),
            {
                "telegram_user_id": telegram_user_id,
                "source_message_id": source_message_id,
                "provider": provider,
                "model": model,
                "purpose": purpose,
                "status": status,
                "request_payload": json.dumps(request_payload),
                "response_payload": json.dumps(response_payload),
                "user_snapshot": json.dumps(user_snapshot, default=str),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "usage_cost": usage.get("cost"),
                "error_message": error_message,
            },
        )
        if commit:
            await self.session.commit()
        return int(result.scalar_one())

    async def user_has_offer(self, telegram_user_id: int) -> bool:
        result = await self.session.execute(
            text(
                """
                select offer_shown_at is not null
                from app.telegram_users
                where id = :telegram_user_id
                """
            ),
            {"telegram_user_id": telegram_user_id},
        )
        return bool(result.scalar_one_or_none())

    async def user_needs_analysis(self, telegram_user_id: int) -> bool:
        result = await self.session.execute(
            text(
                """
                select analysis_ai_request_id is null
                from app.telegram_users
                where id = :telegram_user_id
                """
            ),
            {"telegram_user_id": telegram_user_id},
        )
        return bool(result.scalar_one_or_none())

    async def mark_offer_sent(self, telegram_user_id: int) -> None:
        await self.session.execute(
            text(
                """
                update app.telegram_users
                set offer_shown_at = coalesce(offer_shown_at, now()),
                    updated_at = now()
                where id = :telegram_user_id
                """
            ),
            {"telegram_user_id": telegram_user_id},
        )
        await self.session.commit()

    async def save_user_analysis(
        self,
        telegram_user_id: int,
        ai_request_id: int,
        output: dict[str, Any],
    ) -> None:
        await self.session.execute(
            text(
                """
                update app.telegram_users
                set analysis_ai_request_id = :ai_request_id,
                    analysis_output = cast(:analysis_output as jsonb),
                    niche = :niche,
                    revenue_estimate = :revenue_estimate,
                    average_check = :average_check,
                    sales_volume = :sales_volume,
                    main_problem = :main_problem,
                    lead_temperature = :lead_temperature,
                    summary = :summary,
                    confidence = :confidence,
                    analyzed_at = now(),
                    updated_at = now()
                where id = :telegram_user_id
                """
            ),
            {
                "telegram_user_id": telegram_user_id,
                "ai_request_id": ai_request_id,
                "analysis_output": json.dumps(output),
                "niche": output.get("niche"),
                "revenue_estimate": output.get("revenue_estimate"),
                "average_check": output.get("average_check"),
                "sales_volume": output.get("sales_volume"),
                "main_problem": output.get("main_problem"),
                "lead_temperature": output.get("lead_temperature"),
                "summary": output.get("summary"),
                "confidence": output.get("confidence"),
            },
        )
        await self.session.commit()

    async def get_leads_for_export(self) -> list[dict[str, Any]]:
        result = await self.session.execute(
            text(
                """
                select
                  id,
                  lead_at as created_at,
                  telegram_user_id as telegram_id,
                  chat_id,
                  username,
                  coalesce(
                    nullif(btrim(concat_ws(' ', first_name, last_name)), ''),
                    nullif(username, ''),
                    telegram_user_id::text,
                    chat_id::text,
                    'Без имени'
                  ) as name,
                  nullif(btrim(concat_ws(' ', first_name, last_name)), '') as telegram_name,
                  niche,
                  main_problem,
                  average_check,
                  revenue_estimate,
                  sales_volume,
                  lead_temperature,
                  confidence,
                  summary
                from app.telegram_users
                where funnel_stage = 'lead'
                order by lead_at desc nulls last, id desc
                """
            )
        )
        return [dict(row._mapping) for row in result.all()]

    async def get_or_create_offer_token(
        self,
        telegram_user_id: int,
        *,
        commit: bool = True,
    ) -> str:
        result = await self.session.execute(
            text("select offer_token from app.telegram_users where id = :telegram_user_id"),
            {"telegram_user_id": telegram_user_id},
        )
        existing = result.scalar_one_or_none()
        if existing:
            return str(existing)

        for _ in range(5):
            token = make_link_token()
            result = await self.session.execute(
                text(
                    """
                    update app.telegram_users
                    set offer_token = :token, updated_at = now()
                    where id = :telegram_user_id
                      and offer_token is null
                      and not exists (
                        select 1
                        from app.telegram_users
                        where offer_token = :token
                           or offer_legacy_tokens @> array[cast(:token as text)]
                      )
                    returning offer_token
                    """
                ),
                {"telegram_user_id": telegram_user_id, "token": token},
            )
            value = result.scalar_one_or_none()
            if value:
                if commit:
                    await self.session.commit()
                return str(value)

            result = await self.session.execute(
                text("select offer_token from app.telegram_users where id = :telegram_user_id"),
                {"telegram_user_id": telegram_user_id},
            )
            existing = result.scalar_one_or_none()
            if existing:
                return str(existing)
        raise RuntimeError("could not generate unique offer token")

    async def record_offer_click(self, token: str) -> bool:
        result = await self.session.execute(
            text(
                """
                update app.telegram_users
                set offer_first_clicked_at = coalesce(offer_first_clicked_at, now()),
                    offer_last_clicked_at = now(),
                    offer_click_count = offer_click_count + 1,
                    offer_shown_at = coalesce(offer_shown_at, now()),
                    funnel_stage = 'lead',
                    stage_updated_at = case
                      when funnel_stage = 'lead' then stage_updated_at
                      else now()
                    end,
                    lead_at = coalesce(lead_at, now()),
                    ping_claim_token = null,
                    ping_claim_number = null,
                    ping_claimed_at = null,
                    ping_retry_at = null,
                    ping_pending_ai_request_id = null,
                    updated_at = now()
                where offer_token = :token
                   or offer_legacy_tokens @> array[cast(:token as text)]
                returning id
                """
            ),
            {"token": token},
        )
        found = result.scalar_one_or_none() is not None
        if found:
            await self.session.commit()
        return found

    async def claim_due_ping_users(
        self,
        limit: int,
        lease_seconds: int,
    ) -> list[dict[str, Any]]:
        result = await self.session.execute(
            text(
                """
                select
                  u.id as telegram_user_id,
                  u.chat_id,
                  u.username,
                  u.pings_sent_count + 1 as ping_number,
                  u.ping_anchor_at as anchor_at,
                  extract(epoch from (now() - u.ping_anchor_at))::integer / 60 as idle_minutes,
                  u.offer_shown_at is not null as offer_shown,
                  u.ping_pending_ai_request_id,
                  ar.response_payload as pending_response_payload,
                  c.offer_url,
                  c.ping_1_delay_minutes,
                  c.ping_2_delay_minutes,
                  c.ping_3_delay_minutes
                from app.telegram_users u
                cross join app.config c
                left join app.ai_requests ar on ar.id = u.ping_pending_ai_request_id
                where c.id = 1
                  and u.status = 'active'
                  and u.funnel_stage <> 'lead'
                  and u.chat_id is not null
                  and u.started_at is not null
                  and u.pings_sent_count < 3
                  and u.ping_anchor_at is not null
                  and not coalesce(
                    (select bool_value from app.runtime_flags where key = 'client_bot_stopped'),
                    false
                  )
                  and (u.ping_retry_at is null or u.ping_retry_at <= now())
                  and (
                    u.ping_claim_token is null
                    or u.ping_claimed_at < now() - make_interval(secs => :lease_seconds)
                  )
                  and now() >= u.ping_anchor_at + make_interval(
                    mins => case u.pings_sent_count
                      when 0 then c.ping_1_delay_minutes
                      when 1 then c.ping_2_delay_minutes
                      else c.ping_3_delay_minutes
                    end
                  )
                order by u.ping_anchor_at, u.id
                limit :limit
                for update of u skip locked
                """
            ),
            {"limit": limit, "lease_seconds": lease_seconds},
        )
        claimed: list[dict[str, Any]] = []
        for row in result.all():
            item = dict(row._mapping)
            claim_token = uuid4()
            updated = await self.session.execute(
                text(
                    """
                    update app.telegram_users
                    set ping_claim_token = :claim_token,
                        ping_claim_number = :ping_number,
                        ping_claimed_at = now(),
                        ping_retry_at = null,
                        updated_at = now()
                    where id = :telegram_user_id
                    returning id
                    """
                ),
                {
                    "claim_token": claim_token,
                    "ping_number": item["ping_number"],
                    "telegram_user_id": item["telegram_user_id"],
                },
            )
            if updated.scalar_one_or_none() is None:
                continue
            item["claim_token"] = claim_token
            claimed.append(item)
        await self.session.commit()
        return claimed

    async def create_ping_trigger(
        self,
        telegram_user_id: int,
        ping_number: int,
        anchor_at: datetime | str | None,
        delays_minutes: tuple[int, int, int],
    ) -> int:
        return await self.log_message(
            telegram_user_id=telegram_user_id,
            direction="system",
            text_value=None,
            telegram_message_id=None,
            raw_payload={
                "event": "ping_trigger",
                "ping_number": ping_number,
                "anchor_at": str(anchor_at) if anchor_at is not None else None,
                "delay_minutes": delays_minutes[ping_number - 1],
                "delays_minutes": list(delays_minutes),
            },
            message_type="service",
        )

    async def set_pending_ping_ai_request(
        self,
        telegram_user_id: int,
        claim_token: UUID | str,
        ai_request_id: int,
    ) -> bool:
        result = await self.session.execute(
            text(
                """
                update app.telegram_users
                set ping_pending_ai_request_id = :ai_request_id, updated_at = now()
                where id = :telegram_user_id
                  and ping_claim_token = cast(:claim_token as uuid)
                returning id
                """
            ),
            {
                "telegram_user_id": telegram_user_id,
                "claim_token": str(claim_token),
                "ai_request_id": ai_request_id,
            },
        )
        found = result.scalar_one_or_none() is not None
        await self.session.commit()
        return found

    async def validate_ping_claim(
        self,
        telegram_user_id: int,
        claim_token: UUID | str,
    ) -> dict[str, Any] | None:
        result = await self.session.execute(
            text(
                """
                select
                  u.id as telegram_user_id,
                  u.chat_id,
                  u.ping_claim_number as ping_number,
                  u.ping_anchor_at as anchor_at,
                  u.offer_shown_at is not null as offer_shown,
                  u.ping_pending_ai_request_id,
                  ar.response_payload as pending_response_payload,
                  c.offer_url,
                  c.ping_1_delay_minutes,
                  c.ping_2_delay_minutes,
                  c.ping_3_delay_minutes
                from app.telegram_users u
                cross join app.config c
                left join app.ai_requests ar on ar.id = u.ping_pending_ai_request_id
                where u.id = :telegram_user_id
                  and c.id = 1
                  and u.ping_claim_token = cast(:claim_token as uuid)
                  and u.status = 'active'
                  and u.funnel_stage <> 'lead'
                  and u.pings_sent_count < 3
                  and u.ping_claim_number = u.pings_sent_count + 1
                  and not coalesce(
                    (select bool_value from app.runtime_flags where key = 'client_bot_stopped'),
                    false
                  )
                  and now() >= u.ping_anchor_at + make_interval(
                    mins => case u.ping_claim_number
                      when 1 then c.ping_1_delay_minutes
                      when 2 then c.ping_2_delay_minutes
                      else c.ping_3_delay_minutes
                    end
                  )
                for update of u
                """
            ),
            {"telegram_user_id": telegram_user_id, "claim_token": str(claim_token)},
        )
        row = result.one_or_none()
        return dict(row._mapping) if row else None

    async def complete_ping_send(
        self,
        telegram_user_id: int,
        claim_token: UUID | str,
        ping_number: int,
        ai_request_id: int,
        text_value: str,
        telegram_message_id: int | None,
        raw_payload: dict[str, Any],
        message_type: str = "text",
        sent_at: datetime | None = None,
    ) -> bool:
        if ping_number not in (1, 2, 3):
            raise ValueError("ping_number must be between 1 and 3")
        result = await self.session.execute(
            text(
                f"""
                update app.telegram_users
                set pings_sent_count = :ping_number,
                    ping_{ping_number}_sent_at = coalesce(
                      ping_{ping_number}_sent_at,
                      :sent_at,
                      now()
                    ),
                    ping_claim_token = null,
                    ping_claim_number = null,
                    ping_claimed_at = null,
                    ping_retry_at = null,
                    ping_pending_ai_request_id = null,
                    updated_at = now()
                where id = :telegram_user_id
                  and ping_claim_token = cast(:claim_token as uuid)
                  and ping_claim_number = :ping_number
                  and ping_pending_ai_request_id = :ai_request_id
                  and pings_sent_count = :previous_count
                  and status = 'active'
                  and funnel_stage <> 'lead'
                returning id
                """
            ),
            {
                "telegram_user_id": telegram_user_id,
                "claim_token": str(claim_token),
                "ping_number": ping_number,
                "previous_count": ping_number - 1,
                "ai_request_id": ai_request_id,
                "sent_at": sent_at,
            },
        )
        if result.scalar_one_or_none() is None:
            await self.session.rollback()
            return False
        await self.session.execute(
            text(
                """
                insert into app.messages (
                  telegram_user_id, ai_request_id, direction, message_type, text,
                  telegram_message_id, raw_payload
                )
                values (
                  :telegram_user_id, :ai_request_id, 'outgoing', :message_type, :text,
                  :telegram_message_id, cast(:raw_payload as jsonb)
                )
                """
            ),
            {
                "telegram_user_id": telegram_user_id,
                "ai_request_id": ai_request_id,
                "message_type": message_type,
                "text": text_value,
                "telegram_message_id": telegram_message_id,
                "raw_payload": json.dumps(raw_payload),
            },
        )
        await self.session.commit()
        return True

    async def release_ping_claim(
        self,
        telegram_user_id: int,
        claim_token: UUID | str,
        retry_at: datetime | None = None,
        *,
        preserve_pending: bool = False,
    ) -> bool:
        result = await self.session.execute(
            text(
                """
                update app.telegram_users
                set ping_claim_token = null,
                    ping_claim_number = null,
                    ping_claimed_at = null,
                    ping_retry_at = :retry_at,
                    ping_pending_ai_request_id = case
                      when :preserve_pending then ping_pending_ai_request_id
                      else null
                    end,
                    updated_at = now()
                where id = :telegram_user_id
                  and ping_claim_token = cast(:claim_token as uuid)
                returning id
                """
            ),
            {
                "telegram_user_id": telegram_user_id,
                "claim_token": str(claim_token),
                "retry_at": retry_at,
                "preserve_pending": preserve_pending,
            },
        )
        found = result.scalar_one_or_none() is not None
        await self.session.commit()
        return found

    async def fail_ping_delivery(
        self,
        telegram_user_id: int,
        claim_token: UUID | str,
        *,
        user_status: str | None = None,
        retry_at: datetime | None = None,
        preserve_pending: bool = False,
    ) -> bool:
        result = await self.session.execute(
            text(
                """
                update app.telegram_users
                set status = coalesce(:user_status, status),
                    ping_anchor_at = case
                      when :user_status in ('blocked', 'invalid') then null
                      else ping_anchor_at
                    end,
                    ping_claim_token = null,
                    ping_claim_number = null,
                    ping_claimed_at = null,
                    ping_retry_at = case
                      when :user_status in ('blocked', 'invalid') then null
                      else :retry_at
                    end,
                    ping_pending_ai_request_id = case
                      when :preserve_pending and :user_status is null then ping_pending_ai_request_id
                      else null
                    end,
                    updated_at = now()
                where id = :telegram_user_id
                  and ping_claim_token = cast(:claim_token as uuid)
                returning id
                """
            ),
            {
                "telegram_user_id": telegram_user_id,
                "claim_token": str(claim_token),
                "user_status": user_status,
                "retry_at": retry_at,
                "preserve_pending": preserve_pending,
            },
        )
        found = result.scalar_one_or_none() is not None
        await self.session.commit()
        return found

    async def create_alert(
        self,
        severity: str,
        category: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> int:
        result = await self.session.execute(
            text(
                """
                insert into app.alerts (severity, category, message, details)
                values (:severity, :category, :message, cast(:details as jsonb))
                returning id
                """
            ),
            {
                "severity": severity,
                "category": category,
                "message": message,
                "details": json.dumps(details or {}),
            },
        )
        await self.session.commit()
        return int(result.scalar_one())

    async def is_client_bot_stopped(self) -> bool:
        result = await self.session.execute(
            text(
                """
                select coalesce(
                  (select bool_value from app.runtime_flags where key = 'client_bot_stopped'),
                  false
                )
                """
            )
        )
        return bool(result.scalar_one())

    async def set_client_bot_stopped(self, stopped: bool) -> None:
        await self.session.execute(
            text(
                """
                insert into app.runtime_flags (key, bool_value, updated_at)
                values ('client_bot_stopped', :stopped, now())
                on conflict (key) do update set
                  bool_value = excluded.bool_value,
                  updated_at = now()
                """
            ),
            {"stopped": stopped},
        )
        await self.session.commit()

    async def mark_alert_delivered(self, alert_id: int, chat_id: int) -> None:
        await self.session.execute(
            text(
                """
                update app.alerts
                set delivered_to_chat_id = :chat_id, delivered_at = now()
                where id = :alert_id
                """
            ),
            {"alert_id": alert_id, "chat_id": chat_id},
        )
        await self.session.commit()

    async def get_stats(self) -> dict[str, Any]:
        overall_result = await self.session.execute(
            text(
                """
                select
                  count(*) as total_users,
                  count(*) filter (where started_at is not null) as started_users,
                  count(*) filter (where dialogue_started_at is not null) as dialogue_users,
                  count(*) filter (
                    where status = 'active' and funnel_stage = 'dialogue'
                  ) as in_progress_users,
                  count(*) filter (where ping_1_sent_at is not null) as ping_1_sent_users,
                  count(*) filter (where ping_1_answered_at is not null) as ping_1_answered_users,
                  count(*) filter (where ping_2_sent_at is not null) as ping_2_sent_users,
                  count(*) filter (where ping_2_answered_at is not null) as ping_2_answered_users,
                  count(*) filter (where ping_3_sent_at is not null) as ping_3_sent_users,
                  count(*) filter (where ping_3_answered_at is not null) as ping_3_answered_users,
                  count(*) filter (where funnel_stage = 'lead') as total_leads,
                  coalesce(sum(offer_click_count), 0) as button_clicks
                from app.telegram_users
                where started_at is not null
                """
            )
        )
        daily_result = await self.session.execute(
            text(
                """
                with days as (
                  select generate_series(
                    (now() at time zone 'Europe/Moscow')::date - 13,
                    (now() at time zone 'Europe/Moscow')::date,
                    interval '1 day'
                  )::date as day
                )
                select
                  d.day as date,
                  count(u.id) as started_users,
                  count(u.id) filter (where u.dialogue_started_at is not null) as dialogue_users,
                  count(u.id) filter (
                    where u.status = 'active' and u.funnel_stage = 'dialogue'
                  ) as in_progress_users,
                  count(u.id) filter (where u.ping_1_sent_at is not null) as ping_1_sent_users,
                  count(u.id) filter (where u.ping_1_answered_at is not null) as ping_1_answered_users,
                  count(u.id) filter (where u.ping_2_sent_at is not null) as ping_2_sent_users,
                  count(u.id) filter (where u.ping_2_answered_at is not null) as ping_2_answered_users,
                  count(u.id) filter (where u.ping_3_sent_at is not null) as ping_3_sent_users,
                  count(u.id) filter (where u.ping_3_answered_at is not null) as ping_3_answered_users,
                  count(u.id) filter (where u.funnel_stage = 'lead') as total_leads
                from days d
                left join app.telegram_users u
                  on (u.started_at at time zone 'Europe/Moscow')::date = d.day
                group by d.day
                order by d.day desc
                """
            )
        )
        ai = await self.session.execute(
            text("select coalesce(sum(usage_cost), 0)::text as ai_cost from app.ai_requests")
        )
        overall = dict(overall_result.one()._mapping)
        overall["ai_cost_usd"] = ai.scalar_one()
        daily = []
        for row in daily_result.all():
            values = dict(row._mapping)
            cohort_date = values.pop("date")
            daily.append({"date": cohort_date, "metrics": values})
        return {
            "overall": overall,
            "daily": daily,
        }
