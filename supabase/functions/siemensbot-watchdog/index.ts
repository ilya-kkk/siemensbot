import postgres from "npm:postgres@3.4.7";

const required = (name: string): string => {
  const value = Deno.env.get(name);
  if (!value) throw new Error(`${name} is required`);
  return value;
};

const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

Deno.serve(async (request: Request) => {
  const expectedSecret = required("WATCHDOG_SECRET");
  if (request.headers.get("x-watchdog-secret") !== expectedSecret) {
    return jsonResponse({ error: "unauthorized" }, 401);
  }

  const databaseUrl = required("SUPABASE_DB_URL");
  const publicBaseUrl = required("PUBLIC_BASE_URL").replace(/\/$/, "");
  const adminBotToken = required("ADMIN_BOT_TOKEN");
  const techUsername = required("TECH_ADMIN_USERNAME").trim().replace(/^@/, "").toLowerCase();
  const sql = postgres(databaseUrl, { max: 1, prepare: false });

  try {
    await sql`
      insert into app.service_heartbeats (component, instance_id, status, details, updated_at)
      values ('supabase_watchdog', 'supabase-edge', 'ok', '{}'::jsonb, now())
      on conflict (component) do update set
        instance_id = excluded.instance_id,
        status = excluded.status,
        details = excluded.details,
        updated_at = now()
    `;

    let healthy = false;
    let reason = "Public health endpoint did not respond";
    try {
      const response = await fetch(`${publicBaseUrl}/health`, {
        signal: AbortSignal.timeout(10_000),
        headers: { "user-agent": "siemensbot-supabase-watchdog/1.0" },
      });
      healthy = response.ok;
      if (!healthy) {
        const body = await response.text();
        reason = `Public health returned HTTP ${response.status}: ${body.slice(0, 400)}`;
      }
    } catch (error) {
      reason = `Public health request failed: ${String(error).slice(0, 400)}`;
    }

    const rows = await sql`
      select is_open, last_notified_at
      from app.monitor_incidents
      where incident_key = 'public_health'
    `;
    const incident = rows[0] as { is_open?: boolean; last_notified_at?: Date } | undefined;
    const reminderDue = Boolean(
      incident?.is_open &&
        (!incident.last_notified_at || Date.now() - new Date(incident.last_notified_at).getTime() >= 3_600_000),
    );
    const shouldNotifyDown = !healthy && (!incident?.is_open || reminderDue);
    const shouldNotifyRecovery = healthy && Boolean(incident?.is_open);

    if (shouldNotifyDown || shouldNotifyRecovery) {
      const adminRows = await sql`
        select chat_id
        from app.admin_users
        where username_normalized = ${techUsername}
          and role = 'tech'
          and is_active = true
          and chat_id is not null
        limit 1
      `;
      const chatId = adminRows[0]?.chat_id;
      if (!chatId) throw new Error("Active tech-admin chat ID is unavailable");
      const recovered = shouldNotifyRecovery;
      const text = [
        recovered ? "✅ RECOVERED · production" : "🚨 CRITICAL · production",
        "public_health",
        recovered ? "Public service and all components recovered" : reason,
        `${new Date().toISOString()} UTC`,
      ].join("\n");
      const telegram = await fetch(
        `https://api.telegram.org/bot${adminBotToken}/sendMessage`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ chat_id: chatId, text }),
          signal: AbortSignal.timeout(8_000),
        },
      );
      if (!telegram.ok) throw new Error(`Telegram returned HTTP ${telegram.status}`);
    }

    if (!healthy) {
      await sql`
        insert into app.monitor_incidents (
          incident_key, is_open, opened_at, last_notified_at, details, updated_at
        ) values (
          'public_health', true, now(), now(), ${sql.json({ reason })}, now()
        )
        on conflict (incident_key) do update set
          is_open = true,
          opened_at = coalesce(app.monitor_incidents.opened_at, now()),
          last_notified_at = case
            when ${shouldNotifyDown} then now()
            else app.monitor_incidents.last_notified_at
          end,
          details = excluded.details,
          resolved_at = null,
          updated_at = now()
      `;
    } else if (incident?.is_open) {
      await sql`
        update app.monitor_incidents
        set is_open = false, resolved_at = now(), updated_at = now()
        where incident_key = 'public_health'
      `;
    }

    return jsonResponse({ status: healthy ? "ok" : "degraded" });
  } catch (error) {
    console.error(error);
    return jsonResponse({ error: "watchdog_failed" }, 500);
  } finally {
    await sql.end();
  }
});
