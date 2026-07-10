import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.campaigns import RecipientAssignment, schedule_batches
from app.services.importer import ImportedUser
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
            {"username": username.lstrip("@"), "normalized": normalized, "role": role, "chat_id": chat_id},
        )
        admin_id = int(result.scalar_one())
        await self.session.commit()
        return admin_id

    async def get_tech_admin_chat_id(self) -> int | None:
        result = await self.session.execute(
            text(
                """
                select chat_id
                from app.admin_users
                where role = 'tech' and is_active = true and chat_id is not null
                order by last_seen_at desc nulls last
                limit 1
                """
            )
        )
        value = result.scalar_one_or_none()
        return int(value) if value is not None else None

    async def import_users(self, users: Sequence[ImportedUser]) -> dict[str, int]:
        imported = 0
        unresolved = 0
        for user in users:
            if user.chat_id is None:
                unresolved += 1
                await self.session.execute(
                    text(
                        """
                        insert into app.telegram_users (username, username_normalized, source, status)
                        values (:username, :username_normalized, 'old_import', 'unresolved')
                        on conflict do nothing
                        """
                    ),
                    {"username": user.username, "username_normalized": user.username_normalized},
                )
                continue

            imported += 1
            await self.session.execute(
                text(
                    """
                    insert into app.telegram_users (
                      chat_id, username, username_normalized, source, status, imported_at, first_seen_at
                    )
                    values (
                      :chat_id, :username, :username_normalized, 'old_import', 'active', now(), now()
                    )
                    on conflict (chat_id) do update set
                      username = coalesce(excluded.username, app.telegram_users.username),
                      username_normalized = coalesce(excluded.username_normalized, app.telegram_users.username_normalized),
                      status = case
                        when app.telegram_users.status = 'unresolved' then 'active'
                        else app.telegram_users.status
                      end,
                      updated_at = now()
                    """
                ),
                {
                    "chat_id": user.chat_id,
                    "username": user.username,
                    "username_normalized": user.username_normalized,
                },
            )
        await self.session.commit()
        return {"imported": imported, "unresolved": unresolved, "total": len(users)}

    async def upsert_telegram_user(
        self,
        chat_id: int,
        telegram_user_id: int | None,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        source: str = "new_start",
    ) -> int:
        result = await self.session.execute(
            text(
                """
                insert into app.telegram_users (
                  chat_id, telegram_user_id, username, username_normalized, first_name, last_name,
                  source, status, first_seen_at, last_seen_at
                )
                values (
                  :chat_id, :telegram_user_id, :username, :username_normalized, :first_name, :last_name,
                  :source, 'active', now(), now()
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
                  last_seen_at = now(),
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
                "source": source,
            },
        )
        return int(result.scalar_one())

    async def get_active_old_user_ids(self) -> list[int]:
        result = await self.session.execute(
            text(
                """
                select id
                from app.telegram_users
                where source = 'old_import' and status = 'active' and chat_id is not null
                order by id
                """
            )
        )
        return [int(row[0]) for row in result.all()]

    async def create_campaign(
        self,
        name: str,
        followup_text: str,
        batch_size: int,
        interval_minutes: int,
        created_by_admin_id: int | None,
    ) -> int:
        user_ids = await self.get_active_old_user_ids()
        result = await self.session.execute(
            text(
                """
                insert into app.campaigns (
                  name, followup_text, batch_size, interval_minutes, status, created_by_admin_id, started_at
                )
                values (:name, :followup_text, :batch_size, :interval_minutes, 'running', :admin_id, now())
                returning id
                """
            ),
            {
                "name": name,
                "followup_text": followup_text,
                "batch_size": batch_size,
                "interval_minutes": interval_minutes,
                "admin_id": created_by_admin_id,
            },
        )
        campaign_id = int(result.scalar_one())
        assignments = schedule_batches(user_ids, batch_size, interval_minutes)
        await self._insert_recipients(campaign_id, assignments)
        await self.session.commit()
        return campaign_id

    async def get_current_campaign(self) -> dict[str, Any] | None:
        result = await self.session.execute(
            text(
                """
                select
                  c.id,
                  c.name,
                  c.status,
                  c.batch_size,
                  c.interval_minutes,
                  c.started_at,
                  c.paused_at,
                  c.created_at,
                  count(cr.id) as total_recipients,
                  count(cr.id) filter (where cr.status = 'sent') as sent,
                  count(cr.id) filter (where cr.status in ('pending', 'processing', 'rescheduled')) as pending,
                  count(cr.id) filter (where cr.status = 'failed') as failed
                from app.campaigns c
                left join app.campaign_recipients cr on cr.campaign_id = c.id
                where c.status in ('running', 'paused')
                group by c.id
                order by c.id desc
                limit 1
                """
            )
        )
        row = result.one_or_none()
        return dict(row._mapping) if row else None

    async def configure_current_campaign(
        self,
        batch_size: int,
        interval_minutes: int,
        created_by_admin_id: int | None,
        followup_text: str,
    ) -> tuple[int, str]:
        campaign = await self.get_current_campaign()
        if campaign:
            updated = await self.reconfigure_campaign(int(campaign["id"]), batch_size, interval_minutes)
            return int(campaign["id"]), f"updated:{updated}"

        campaign_id = await self.create_campaign(
            name="follow-up",
            followup_text=followup_text,
            batch_size=batch_size,
            interval_minutes=interval_minutes,
            created_by_admin_id=created_by_admin_id,
        )
        await self.set_client_bot_stopped(False)
        return campaign_id, "created"

    async def _insert_recipients(self, campaign_id: int, assignments: Sequence[RecipientAssignment]) -> None:
        for assignment in assignments:
            await self.session.execute(
                text(
                    """
                    insert into app.campaign_recipients (
                      campaign_id, telegram_user_id, batch_number, scheduled_at, status
                    )
                    values (:campaign_id, :telegram_user_id, :batch_number, :scheduled_at, 'pending')
                    on conflict (campaign_id, telegram_user_id) do update set
                      batch_number = excluded.batch_number,
                      scheduled_at = excluded.scheduled_at,
                      updated_at = now()
                    where app.campaign_recipients.status in ('pending', 'rescheduled')
                    """
                ),
                {
                    "campaign_id": campaign_id,
                    "telegram_user_id": assignment.telegram_user_id,
                    "batch_number": assignment.batch_number,
                    "scheduled_at": assignment.scheduled_at,
                },
            )

    async def pause_running_campaigns(self) -> int:
        result = await self.session.execute(
            text(
                """
                update app.campaigns
                set status = 'paused', paused_at = now(), updated_at = now()
                where status = 'running'
                returning id
                """
            )
        )
        rows = result.all()
        await self.session.commit()
        return len(rows)

    async def pause_current_campaign(self) -> int | None:
        result = await self.session.execute(
            text(
                """
                update app.campaigns
                set status = 'paused', paused_at = now(), updated_at = now()
                where id = (
                  select id
                  from app.campaigns
                  where status = 'running'
                  order by id desc
                  limit 1
                )
                returning id
                """
            )
        )
        value = result.scalar_one_or_none()
        await self.session.commit()
        return int(value) if value is not None else None

    async def resume_current_campaign(self) -> int | None:
        await self.set_client_bot_stopped(False)
        result = await self.session.execute(
            text(
                """
                update app.campaigns
                set status = 'running', paused_at = null, updated_at = now()
                where id = (
                  select id
                  from app.campaigns
                  where status = 'paused'
                  order by id desc
                  limit 1
                )
                returning id
                """
            )
        )
        value = result.scalar_one_or_none()
        await self.session.commit()
        return int(value) if value is not None else None

    async def resume_campaign(self, campaign_id: int) -> None:
        await self.session.execute(
            text(
                """
                update app.campaigns
                set status = 'running', paused_at = null, updated_at = now()
                where id = :campaign_id and status in ('paused', 'draft')
                """
            ),
            {"campaign_id": campaign_id},
        )
        await self.session.commit()

    async def release_recipient(self, recipient_id: int) -> None:
        await self.session.execute(
            text(
                """
                update app.campaign_recipients
                set status = 'pending',
                    locked_at = null,
                    updated_at = now()
                where id = :recipient_id and status = 'processing'
                """
            ),
            {"recipient_id": recipient_id},
        )
        await self.session.commit()

    async def reconfigure_campaign(self, campaign_id: int, batch_size: int, interval_minutes: int) -> int:
        result = await self.session.execute(
            text(
                """
                select id
                from app.campaign_recipients
                where campaign_id = :campaign_id and status in ('pending', 'rescheduled')
                order by id
                """
            ),
            {"campaign_id": campaign_id},
        )
        recipient_ids = [int(row[0]) for row in result.all()]
        start_at = datetime.now(UTC)
        assignments = schedule_batches(recipient_ids, batch_size, interval_minutes, start_at=start_at)
        for assignment in assignments:
            await self.session.execute(
                text(
                    """
                    update app.campaign_recipients
                    set batch_number = :batch_number,
                        scheduled_at = :scheduled_at,
                        updated_at = now()
                    where id = :recipient_row_id and status in ('pending', 'rescheduled')
                    """
                ),
                {
                    "recipient_row_id": assignment.telegram_user_id,
                    "batch_number": assignment.batch_number,
                    "scheduled_at": assignment.scheduled_at,
                },
            )
        await self.session.execute(
            text(
                """
                update app.campaigns
                set batch_size = :batch_size, interval_minutes = :interval_minutes, updated_at = now()
                where id = :campaign_id
                """
            ),
            {"campaign_id": campaign_id, "batch_size": batch_size, "interval_minutes": interval_minutes},
        )
        await self.session.commit()
        return len(recipient_ids)

    async def claim_due_recipients(self, limit: int) -> list[dict[str, Any]]:
        result = await self.session.execute(
            text(
                """
                with claimed as (
                  select cr.id
                  from app.campaign_recipients cr
                  join app.campaigns c on c.id = cr.campaign_id
                  join app.telegram_users u on u.id = cr.telegram_user_id
                  where cr.status in ('pending', 'rescheduled')
                    and cr.scheduled_at <= now()
                    and c.status = 'running'
                    and u.status = 'active'
                    and u.chat_id is not null
                  order by cr.scheduled_at, cr.id
                  limit :limit
                  for update of cr skip locked
                )
                update app.campaign_recipients cr
                set status = 'processing',
                    attempts = cr.attempts + 1,
                    locked_at = now(),
                    updated_at = now()
                from claimed
                join app.campaign_recipients picked on picked.id = claimed.id
                join app.campaigns c on c.id = picked.campaign_id
                join app.telegram_users u on u.id = picked.telegram_user_id
                where cr.id = claimed.id
                returning
                  cr.id as recipient_id,
                  cr.campaign_id,
                  cr.telegram_user_id,
                  c.followup_text,
                  u.chat_id,
                  u.username
                """
            ),
            {"limit": limit},
        )
        await self.session.commit()
        return [dict(row._mapping) for row in result.all()]

    async def mark_recipient_sent(self, recipient_id: int, telegram_message_id: int | None) -> None:
        await self.session.execute(
            text(
                """
                update app.campaign_recipients
                set status = 'sent',
                    sent_at = now(),
                    telegram_message_id = :telegram_message_id,
                    updated_at = now()
                where id = :recipient_id
                """
            ),
            {"recipient_id": recipient_id, "telegram_message_id": telegram_message_id},
        )
        await self.session.commit()

    async def mark_recipient_error(
        self,
        recipient_id: int,
        telegram_user_id: int,
        recipient_status: str,
        user_status: str | None,
        error_code: int | None,
        description: str,
        retry_after_seconds: int | None,
    ) -> None:
        scheduled_at = (
            datetime.now(UTC) + timedelta(seconds=retry_after_seconds)
            if recipient_status == "rescheduled" and retry_after_seconds
            else None
        )
        await self.session.execute(
            text(
                """
                update app.campaign_recipients
                set status = :status,
                    telegram_error_code = :error_code,
                    telegram_error_description = :description,
                    retry_after_seconds = :retry_after_seconds,
                    scheduled_at = coalesce(:scheduled_at, scheduled_at),
                    updated_at = now()
                where id = :recipient_id
                """
            ),
            {
                "recipient_id": recipient_id,
                "status": recipient_status,
                "error_code": error_code,
                "description": description,
                "retry_after_seconds": retry_after_seconds,
                "scheduled_at": scheduled_at,
            },
        )
        if user_status:
            await self.session.execute(
                text("update app.telegram_users set status = :status, updated_at = now() where id = :user_id"),
                {"status": user_status, "user_id": telegram_user_id},
            )
        await self.session.commit()

    async def get_or_create_dialogue(self, telegram_user_id: int) -> int:
        result = await self.session.execute(
            text(
                """
                select id from app.dialogues
                where telegram_user_id = :telegram_user_id and status in ('active', 'offer_sent')
                order by id desc
                limit 1
                """
            ),
            {"telegram_user_id": telegram_user_id},
        )
        existing = result.scalar_one_or_none()
        if existing is not None:
            return int(existing)
        result = await self.session.execute(
            text("insert into app.dialogues (telegram_user_id) values (:telegram_user_id) returning id"),
            {"telegram_user_id": telegram_user_id},
        )
        await self.session.commit()
        return int(result.scalar_one())

    async def log_message(
        self,
        telegram_user_id: int,
        dialogue_id: int,
        direction: str,
        text_value: str | None,
        telegram_message_id: int | None,
        raw_payload: dict[str, Any] | None = None,
        message_type: str = "text",
    ) -> int:
        result = await self.session.execute(
            text(
                """
                insert into app.messages (
                  dialogue_id, telegram_user_id, direction, message_type, text, telegram_message_id, raw_payload
                )
                values (
                  :dialogue_id, :telegram_user_id, :direction, :message_type, :text,
                  :telegram_message_id, cast(:raw_payload as jsonb)
                )
                returning id
                """
            ),
            {
                "dialogue_id": dialogue_id,
                "telegram_user_id": telegram_user_id,
                "direction": direction,
                "message_type": message_type,
                "text": text_value,
                "telegram_message_id": telegram_message_id,
                "raw_payload": json.dumps(raw_payload or {}),
            },
        )
        await self.session.commit()
        return int(result.scalar_one())

    async def get_dialogue_messages(self, query: str, limit: int = 80) -> list[dict[str, Any]]:
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
                select m.created_at, m.direction, m.text
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

    async def get_transcript_for_analysis(self, dialogue_id: int) -> str:
        result = await self.session.execute(
            text(
                """
                select direction, text
                from app.messages
                where dialogue_id = :dialogue_id and text is not null
                order by created_at, id
                """
            ),
            {"dialogue_id": dialogue_id},
        )
        return "\n".join(f"{row.direction}: {row.text}" for row in result.all())

    async def has_dialogue_analysis(self, dialogue_id: int) -> bool:
        result = await self.session.execute(
            text(
                """
                select exists (
                  select 1
                  from app.dialogue_analyses
                  where dialogue_id = :dialogue_id
                )
                """
            ),
            {"dialogue_id": dialogue_id},
        )
        return bool(result.scalar_one())

    async def save_ai_request(
        self,
        telegram_user_id: int | None,
        dialogue_id: int | None,
        purpose: str,
        model: str,
        status: str,
        request_payload: dict[str, Any],
        response_payload: dict[str, Any],
        usage: dict[str, Any] | None,
        error_message: str | None = None,
    ) -> int:
        usage = usage or {}
        result = await self.session.execute(
            text(
                """
                insert into app.ai_requests (
                  telegram_user_id, dialogue_id, purpose, model, status, request_payload, response_payload,
                  prompt_tokens, completion_tokens, total_tokens, usage_cost, error_message
                )
                values (
                  :telegram_user_id, :dialogue_id, :purpose, :model, :status,
                  cast(:request_payload as jsonb), cast(:response_payload as jsonb),
                  :prompt_tokens, :completion_tokens, :total_tokens, :usage_cost, :error_message
                )
                returning id
                """
            ),
            {
                "telegram_user_id": telegram_user_id,
                "dialogue_id": dialogue_id,
                "purpose": purpose,
                "model": model,
                "status": status,
                "request_payload": json.dumps(request_payload),
                "response_payload": json.dumps(response_payload),
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "usage_cost": usage.get("cost"),
                "error_message": error_message,
            },
        )
        await self.session.commit()
        return int(result.scalar_one())

    async def create_lead(self, telegram_user_id: int, dialogue_id: int, contact_name: str) -> int:
        result = await self.session.execute(
            text(
                """
                insert into app.leads (
                  telegram_user_id, dialogue_id, telegram_id, chat_id, username, first_name, last_name,
                  contact_name, niche, revenue_estimate, average_check, sales_volume, main_problem,
                  lead_temperature, summary, confidence, structured_output
                )
                select
                  u.id,
                  :dialogue_id,
                  u.telegram_user_id,
                  u.chat_id,
                  u.username,
                  u.first_name,
                  u.last_name,
                  :contact_name,
                  da.niche,
                  da.revenue_estimate,
                  da.average_check,
                  da.sales_volume,
                  da.main_problem,
                  da.structured_output ->> 'lead_temperature',
                  da.structured_output ->> 'summary',
                  da.confidence,
                  coalesce(da.structured_output, '{}'::jsonb)
                from app.telegram_users u
                left join app.dialogue_analyses da on da.dialogue_id = :dialogue_id
                where u.id = :telegram_user_id
                on conflict (dialogue_id) do update set
                  telegram_user_id = excluded.telegram_user_id,
                  telegram_id = coalesce(excluded.telegram_id, app.leads.telegram_id),
                  chat_id = coalesce(excluded.chat_id, app.leads.chat_id),
                  username = coalesce(excluded.username, app.leads.username),
                  first_name = coalesce(excluded.first_name, app.leads.first_name),
                  last_name = coalesce(excluded.last_name, app.leads.last_name),
                  contact_name = excluded.contact_name,
                  niche = coalesce(excluded.niche, app.leads.niche),
                  revenue_estimate = coalesce(excluded.revenue_estimate, app.leads.revenue_estimate),
                  average_check = coalesce(excluded.average_check, app.leads.average_check),
                  sales_volume = coalesce(excluded.sales_volume, app.leads.sales_volume),
                  main_problem = coalesce(excluded.main_problem, app.leads.main_problem),
                  lead_temperature = coalesce(excluded.lead_temperature, app.leads.lead_temperature),
                  summary = coalesce(excluded.summary, app.leads.summary),
                  confidence = coalesce(excluded.confidence, app.leads.confidence),
                  structured_output = case
                    when excluded.structured_output = '{}'::jsonb then app.leads.structured_output
                    else excluded.structured_output
                  end,
                  updated_at = now()
                returning id
                """
            ),
            {
                "telegram_user_id": telegram_user_id,
                "dialogue_id": dialogue_id,
                "contact_name": contact_name,
            },
        )
        await self.session.commit()
        return int(result.scalar_one())

    async def get_leads_for_export(self) -> list[dict[str, Any]]:
        result = await self.session.execute(
            text(
                """
                select
                  id,
                  created_at,
                  telegram_id,
                  chat_id,
                  username,
                  contact_name as name,
                  nullif(btrim(concat_ws(' ', first_name, last_name)), '') as telegram_name,
                  niche,
                  main_problem,
                  average_check,
                  revenue_estimate,
                  sales_volume,
                  lead_temperature,
                  confidence,
                  summary
                from app.leads
                order by created_at desc, id desc
                """
            )
        )
        return [dict(row._mapping) for row in result.all()]

    async def mark_offer_sent(self, dialogue_id: int) -> None:
        await self.session.execute(
            text(
                """
                update app.dialogues
                set status = 'offer_sent', offer_sent_at = now(), updated_at = now()
                where id = :dialogue_id and status = 'active'
                """
            ),
            {"dialogue_id": dialogue_id},
        )
        await self.session.commit()

    async def save_dialogue_analysis(
        self,
        telegram_user_id: int,
        dialogue_id: int,
        ai_request_id: int,
        output: dict[str, Any],
    ) -> None:
        await self.session.execute(
            text(
                """
                insert into app.dialogue_analyses (
                  telegram_user_id, dialogue_id, ai_request_id, structured_output,
                  niche, revenue_estimate, average_check, sales_volume, main_problem, confidence
                )
                values (
                  :telegram_user_id, :dialogue_id, :ai_request_id, cast(:structured_output as jsonb),
                  :niche, :revenue_estimate, :average_check, :sales_volume, :main_problem, :confidence
                )
                on conflict (dialogue_id) do update set
                  ai_request_id = excluded.ai_request_id,
                  structured_output = excluded.structured_output,
                  niche = excluded.niche,
                  revenue_estimate = excluded.revenue_estimate,
                  average_check = excluded.average_check,
                  sales_volume = excluded.sales_volume,
                  main_problem = excluded.main_problem,
                  confidence = excluded.confidence,
                  created_at = now()
                """
            ),
            {
                "telegram_user_id": telegram_user_id,
                "dialogue_id": dialogue_id,
                "ai_request_id": ai_request_id,
                "structured_output": json.dumps(output),
                "niche": output.get("niche"),
                "revenue_estimate": output.get("revenue_estimate"),
                "average_check": output.get("average_check"),
                "sales_volume": output.get("sales_volume"),
                "main_problem": output.get("main_problem"),
                "confidence": output.get("confidence"),
            },
        )
        await self.session.execute(
            text(
                """
                update app.dialogues
                set status = 'analyzed', analysis_completed_at = now(), updated_at = now()
                where id = :dialogue_id
                """
            ),
            {"dialogue_id": dialogue_id},
        )
        await self.session.commit()

    async def create_tracked_link(self, telegram_user_id: int, dialogue_id: int | None, destination_url: str) -> str:
        for _ in range(5):
            token = make_link_token()
            result = await self.session.execute(
                text(
                    """
                    insert into app.tracked_links (telegram_user_id, dialogue_id, token, destination_url)
                    values (:telegram_user_id, :dialogue_id, :token, :destination_url)
                    on conflict (token) do nothing
                    returning token
                    """
                ),
                {
                    "telegram_user_id": telegram_user_id,
                    "dialogue_id": dialogue_id,
                    "token": token,
                    "destination_url": destination_url,
                },
            )
            value = result.scalar_one_or_none()
            if value:
                await self.session.commit()
                return str(value)
        raise RuntimeError("could not generate unique link token")

    async def record_link_click(self, token: str, ip_address: str | None, user_agent: str | None) -> str | None:
        result = await self.session.execute(
            text(
                """
                select id, telegram_user_id, destination_url
                from app.tracked_links
                where token = :token
                """
            ),
            {"token": token},
        )
        row = result.one_or_none()
        if row is None:
            return None
        await self.session.execute(
            text(
                """
                insert into app.link_clicks (tracked_link_id, telegram_user_id, ip_address, user_agent)
                values (:tracked_link_id, :telegram_user_id, cast(:ip_address as inet), :user_agent)
                """
            ),
            {
                "tracked_link_id": row.id,
                "telegram_user_id": row.telegram_user_id,
                "ip_address": ip_address,
                "user_agent": user_agent,
            },
        )
        await self.session.commit()
        return str(row.destination_url)

    async def create_alert(self, severity: str, category: str, message: str, details: dict[str, Any] | None = None) -> int:
        result = await self.session.execute(
            text(
                """
                insert into app.alerts (severity, category, message, details)
                values (:severity, :category, :message, cast(:details as jsonb))
                returning id
                """
            ),
            {"severity": severity, "category": category, "message": message, "details": json.dumps(details or {})},
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

    async def emergency_stop_client_bot(self) -> int:
        await self.session.execute(
            text(
                """
                insert into app.runtime_flags (key, bool_value, updated_at)
                values ('client_bot_stopped', true, now())
                on conflict (key) do update set
                  bool_value = true,
                  updated_at = now()
                """
            )
        )
        result = await self.session.execute(
            text(
                """
                update app.campaigns
                set status = 'paused', paused_at = now(), updated_at = now()
                where status = 'running'
                returning id
                """
            )
        )
        rows = result.all()
        await self.session.commit()
        return len(rows)

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
        users = await self.session.execute(
            text(
                """
                select
                  count(*) as total_users,
                  count(*) filter (where source = 'old_import') as old_users,
                  count(*) filter (where source = 'old_import' and status = 'active' and chat_id is not null) as active_old_users,
                  count(*) filter (where status = 'blocked') as blocked_users,
                  count(*) filter (where status = 'invalid') as invalid_users,
                  count(*) filter (where status = 'unresolved') as unresolved_users
                from app.telegram_users
                """
            )
        )
        campaigns = await self.session.execute(
            text(
                """
                select
                  count(*) as total_recipients,
                  count(*) filter (where status = 'sent') as sent,
                  count(*) filter (where status in ('pending', 'processing', 'rescheduled')) as pending,
                  count(*) filter (where status = 'failed') as failed
                from app.campaign_recipients
                """
            )
        )
        funnel = await self.session.execute(
            text(
                """
                with sent as (
                  select telegram_user_id, min(sent_at) as sent_at
                  from app.campaign_recipients
                  where status = 'sent' and sent_at is not null
                  group by telegram_user_id
                ),
                states as (
                  select
                    s.telegram_user_id,
                    exists (
                      select 1
                      from app.messages m
                      where m.telegram_user_id = s.telegram_user_id
                        and m.direction = 'incoming'
                        and m.message_type = 'text'
                        and m.created_at > s.sent_at
                    ) as replied,
                    exists (
                      select 1
                      from app.dialogues d
                      where d.telegram_user_id = s.telegram_user_id
                        and d.offer_sent_at is not null
                        and d.offer_sent_at > s.sent_at
                    ) as offer_sent,
                    exists (
                      select 1
                      from app.leads l
                      where l.telegram_user_id = s.telegram_user_id
                        and l.created_at > s.sent_at
                    ) as became_lead
                  from sent s
                )
                select
                  count(*) filter (where not replied) as sent_no_reply,
                  count(*) filter (where replied) as replied_users,
                  count(*) filter (where replied and not became_lead) as replied_no_lead,
                  count(*) filter (where offer_sent and not became_lead) as offer_no_lead,
                  count(*) filter (where became_lead) as leads_from_followup
                from states
                """
            )
        )
        leads = await self.session.execute(text("select count(*) from app.leads"))
        delivery_errors = await self.session.execute(
            text(
                """
                select
                  coalesce(telegram_error_code::text, 'unknown') as telegram_error_code,
                  count(*) as count
                from app.campaign_recipients
                where status = 'failed' or telegram_error_code is not null
                group by coalesce(telegram_error_code::text, 'unknown')
                order by count(*) desc
                """
            )
        )
        clicks = await self.session.execute(text("select count(*) from app.link_clicks"))
        ai = await self.session.execute(
            text("select coalesce(sum(usage_cost), 0)::text as ai_cost from app.ai_requests")
        )

        user_stats = dict(users.one()._mapping)
        campaign_stats = dict(campaigns.one()._mapping)
        funnel_stats = dict(funnel.one()._mapping)
        total_recipients = int(campaign_stats["total_recipients"] or 0)
        sent = int(campaign_stats["sent"] or 0)
        return {
            **user_stats,
            **campaign_stats,
            **funnel_stats,
            "sent_percent": round(sent / total_recipients * 100, 2) if total_recipients else 0.0,
            "total_leads": int(leads.scalar_one() or 0),
            "delivery_errors": [dict(row._mapping) for row in delivery_errors.all()],
            "button_clicks": int(clicks.scalar_one() or 0),
            "ai_cost_usd": ai.scalar_one(),
        }
