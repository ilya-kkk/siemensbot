from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

_MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def _as_moscow_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(_MOSCOW_TZ)


def _datetime_label(value: Any) -> str:
    result = _as_moscow_datetime(value)
    return result.strftime("%d.%m.%Y %H:%M") if result else "Время неизвестно"


def _date_label(value: Any) -> str:
    result = _as_moscow_datetime(value)
    return result.strftime("%d.%m.%Y") if result else ""


def _time_label(value: Any) -> str:
    result = _as_moscow_datetime(value)
    return result.strftime("%H:%M") if result else ""


def _user_label(dialogue: Mapping[str, Any]) -> str:
    telegram_name = str(dialogue.get("telegram_name") or "").strip()
    username = str(dialogue.get("username") or "").strip().lstrip("@")
    if telegram_name and username:
        return f"{telegram_name} · @{username}"
    if telegram_name:
        return telegram_name
    if username:
        return f"@{username}"
    telegram_id = dialogue.get("telegram_user_id")
    if telegram_id is not None:
        return f"Пользователь {telegram_id}"
    return f"Пользователь #{dialogue.get('user_record_id', '—')}"


def _avatar_text(dialogue: Mapping[str, Any]) -> str:
    label = _user_label(dialogue).lstrip("@").strip()
    return (label[:1] or "?").upper()


def _stage_label(value: Any) -> str:
    return {
        "started": "Нажал Start",
        "dialogue": "В диалоге",
        "lead": "Лид",
    }.get(str(value or ""), str(value or "Статус неизвестен"))


def _username_html(user: Mapping[str, Any]) -> str:
    username = str(user.get("username") or "").strip().lstrip("@")
    if not username:
        return "username отсутствует"
    return f'<a href="https://t.me/{escape(username, quote=True)}">@{escape(username)}</a>'


def _render_unanswered_users(users: Sequence[Mapping[str, Any]]) -> str:
    if not users:
        return """
          <section class="unanswered">
            <header class="section-title">
              <h2>Не ответили на первое сообщение</h2>
              <span>0 пользователей</span>
            </header>
            <div class="empty-list">Таких пользователей нет</div>
          </section>
        """

    rows = []
    for index, user in enumerate(users, start=1):
        rows.append(
            f"""
            <tr>
              <td>{index}</td>
              <td>
                <div class="user-main">{escape(_user_label(user))}</div>
                <div class="meta">{_username_html(user)} · Telegram ID {escape(str(user.get("telegram_user_id") or "—"))} · chat_id {escape(str(user.get("chat_id") or "—"))}</div>
              </td>
              <td>{escape(_datetime_label(user.get("started_at")))} MSK</td>
              <td>{escape(str(user.get("status") or "—"))}</td>
            </tr>
            """
        )

    return f"""
      <section class="unanswered">
        <header class="section-title">
          <h2>Не ответили на первое сообщение</h2>
          <span>{len(users)} пользователей</span>
        </header>
        <div class="table-wrap">
          <table>
            <thead>
              <tr><th>#</th><th>Пользователь</th><th>Старт</th><th>Статус</th></tr>
            </thead>
            <tbody>{"".join(rows)}</tbody>
          </table>
        </div>
      </section>
    """


def _render_message(message: Mapping[str, Any]) -> str:
    direction = str(message.get("direction") or "system")
    if direction not in {"incoming", "outgoing", "system"}:
        direction = "system"
    sender = {
        "incoming": "Пользователь",
        "outgoing": "Бот",
        "system": "Система",
    }[direction]
    text = message.get("text")
    if text is None or str(text) == "":
        message_type = str(message.get("message_type") or "сообщение")
        rendered_text = f"&lt;{escape(message_type)} без текста&gt;"
    else:
        rendered_text = escape(str(text))
    return (
        f'<div class="message-row {direction}">'
        f'<div class="bubble"><div class="sender">{sender}</div>'
        f'<div class="message-text">{rendered_text}</div>'
        f'<time>{escape(_time_label(message.get("created_at")))}</time></div></div>'
    )


def _render_messages(messages: Sequence[Mapping[str, Any]]) -> str:
    if not messages:
        return '<div class="empty-chat">В этом диалоге нет сохранённых сообщений</div>'

    parts: list[str] = []
    current_date = ""
    for message in messages:
        date_label = _date_label(message.get("created_at"))
        if date_label and date_label != current_date:
            parts.append(f'<div class="date-separator"><span>{escape(date_label)}</span></div>')
            current_date = date_label
        parts.append(_render_message(message))
    return "".join(parts)


def render_dialogues_report_html(
    dialogues: Sequence[Mapping[str, Any]],
    unanswered_users: Sequence[Mapping[str, Any]] | None = None,
    generated_at: datetime | None = None,
) -> bytes:
    """Build a self-contained UTF-8 HTML document with all dialogues."""
    generated_at = generated_at or datetime.now(UTC)
    unanswered_users = unanswered_users or []
    message_count = sum(len(dialogue.get("messages") or []) for dialogue in dialogues)
    sections: list[str] = []

    for index, dialogue in enumerate(dialogues, start=1):
        label = _user_label(dialogue)
        messages = dialogue.get("messages") or []
        user_id = escape(str(dialogue.get("telegram_user_id") or "—"))
        chat_id = escape(str(dialogue.get("chat_id") or "—"))
        sections.append(
            f"""
            <section class="dialogue" id="dialogue-{index}">
              <header class="chat-header">
                <div class="avatar">{escape(_avatar_text(dialogue))}</div>
                <div class="identity">
                  <h2>{escape(label)}</h2>
                  <div class="meta">Диалог #{index} · начат {_datetime_label(dialogue.get("dialogue_started_at"))} MSK</div>
                  <div class="meta">{_username_html(dialogue)} · Telegram ID {user_id} · chat_id {chat_id}</div>
                </div>
                <div class="chat-stats">
                  <span class="stage">{escape(_stage_label(dialogue.get("funnel_stage")))}</span>
                  <span>{len(messages)} сообщ.</span>
                </div>
              </header>
              <div class="chat-body">{_render_messages(messages)}</div>
            </section>
            """
        )

    content = "".join(sections) or """
      <section class="no-dialogues">
        <h2>Диалогов пока нет</h2>
        <p>В отчёте появятся пользователи, которые начали общение с ботом.</p>
      </section>
    """
    unanswered_content = _render_unanswered_users(unanswered_users)
    generated_label = escape(_datetime_label(generated_at))
    document = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Отчёт по диалогам Siemensbot</title>
  <style>
    :root {{ color-scheme: light; --bg: #dce8ef; --panel: #fff; --line: #d7e0e5; --muted: #667781; --accent: #3390ec; --incoming: #fff; --outgoing: #e2ffc7; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: #17212b; font: 15px/1.42 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    .report-header {{ position: sticky; top: 0; z-index: 10; padding: 18px 24px; color: #fff; background: #17212b; box-shadow: 0 2px 12px #0003; }}
    .report-header h1 {{ margin: 0 0 4px; font-size: 22px; }}
    .report-header p {{ margin: 0; color: #c7d3dc; }}
    main {{ width: min(100% - 28px, 980px); margin: 24px auto 60px; }}
    .dialogue {{ margin: 0 0 28px; overflow: hidden; border: 1px solid #cbd7de; border-radius: 14px; background: var(--panel); box-shadow: 0 5px 22px #3449551f; break-inside: avoid-page; }}
    .chat-header {{ display: flex; align-items: center; gap: 12px; padding: 14px 18px; border-bottom: 1px solid var(--line); background: #fff; }}
    .avatar {{ display: grid; flex: 0 0 44px; height: 44px; place-items: center; border-radius: 50%; color: #fff; background: linear-gradient(145deg, #54a9eb, #2979c7); font-size: 18px; font-weight: 700; }}
    .identity {{ min-width: 0; flex: 1; }}
    .identity h2 {{ margin: 0 0 3px; overflow-wrap: anywhere; font-size: 17px; }}
    .meta {{ color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }}
    .meta a {{ color: var(--accent); text-decoration: none; }}
    .section-title {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 16px 18px; border-bottom: 1px solid var(--line); background: #fff; }}
    .section-title h2 {{ margin: 0; font-size: 18px; }}
    .section-title span {{ color: var(--muted); font-size: 13px; white-space: nowrap; }}
    .unanswered {{ margin: 0 0 28px; overflow: hidden; border: 1px solid #cbd7de; border-radius: 14px; background: var(--panel); box-shadow: 0 5px 22px #3449551f; }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 760px; }}
    th, td {{ padding: 11px 14px; border-bottom: 1px solid #edf1f3; text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); background: #f6f8f9; font-size: 12px; font-weight: 700; }}
    tr:last-child td {{ border-bottom: 0; }}
    .user-main {{ margin-bottom: 3px; font-weight: 700; overflow-wrap: anywhere; }}
    .chat-stats {{ display: flex; align-items: flex-end; gap: 6px; flex-direction: column; color: var(--muted); font-size: 12px; white-space: nowrap; }}
    .stage {{ padding: 3px 8px; border-radius: 999px; color: #236132; background: #ddf6df; font-weight: 600; }}
    .chat-body {{ padding: 18px max(14px, 5vw); background-color: #91a5af; background-image: linear-gradient(135deg, #ffffff0d 25%, transparent 25%), linear-gradient(315deg, #ffffff0d 25%, transparent 25%); background-size: 28px 28px; }}
    .message-row {{ display: flex; margin: 5px 0; }}
    .message-row.incoming {{ justify-content: flex-start; }}
    .message-row.outgoing {{ justify-content: flex-end; }}
    .message-row.system {{ justify-content: center; }}
    .bubble {{ position: relative; min-width: 120px; max-width: min(76%, 660px); padding: 7px 52px 7px 10px; border-radius: 11px; background: var(--incoming); box-shadow: 0 1px 1px #0002; }}
    .outgoing .bubble {{ background: var(--outgoing); }}
    .system .bubble {{ min-width: auto; max-width: 80%; padding: 5px 10px; color: #fff; background: #5b6f79cc; text-align: center; }}
    .sender {{ margin-bottom: 2px; color: #2186d4; font-size: 12px; font-weight: 700; }}
    .outgoing .sender {{ color: #3b8c3f; }}
    .system .sender {{ display: none; }}
    .message-text {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
    time {{ position: absolute; right: 8px; bottom: 5px; color: var(--muted); font-size: 11px; }}
    .system time {{ display: none; }}
    .date-separator {{ margin: 14px 0 10px; text-align: center; }}
    .date-separator span {{ display: inline-block; padding: 4px 10px; border-radius: 999px; color: #fff; background: #5b6f79bf; font-size: 12px; font-weight: 600; }}
    .empty-chat, .empty-list, .no-dialogues {{ padding: 38px; color: var(--muted); background: #fff; text-align: center; }}
    @media (max-width: 640px) {{
      .report-header {{ padding: 14px 16px; }} main {{ width: 100%; margin-top: 12px; }} .dialogue {{ border-radius: 0; border-left: 0; border-right: 0; }}
      .chat-header {{ align-items: flex-start; padding: 12px; }} .chat-stats {{ display: none; }} .bubble {{ max-width: 88%; }}
    }}
    @media print {{ .report-header {{ position: static; }} main {{ width: 100%; margin: 12px 0; }} .dialogue {{ box-shadow: none; }} }}
  </style>
</head>
<body>
  <header class="report-header">
    <h1>Все диалоги</h1>
    <p>{len(dialogues)} диалогов · {message_count} сообщений · без ответа {len(unanswered_users)} · сформирован {generated_label} MSK · от старых к новым</p>
  </header>
  <main>{unanswered_content}{content}</main>
</body>
</html>
"""
    return document.encode("utf-8")
