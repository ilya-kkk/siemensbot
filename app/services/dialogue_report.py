from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

_MOSCOW_TZ = ZoneInfo("Europe/Moscow")

_LEAD_OPTIONS = (
    ("target", "Целевой"),
    ("non_target", "Нецелевой"),
    ("not_enough_data", "Пока недостаточно данных"),
    ("unsure", "Не уверен"),
)
_RESPONSE_OPTIONS = (
    ("yes", "Да"),
    ("no", "Нет"),
    ("unsure", "Не уверен"),
)
_BUTTON_OPTIONS = (
    ("yes", "Да"),
    ("no", "Нет"),
    ("need_more_qualification", "Сначала нужна квалификация"),
    ("unsure", "Не уверен"),
)
_FAILURE_TAGS = (
    ("wrong_next_step", "Неверный следующий шаг или вопрос"),
    ("repeats_known_information", "Повторно спрашивает уже известное"),
    ("ignored_context", "Игнорирует контекст диалога"),
    ("unsupported_conclusion", "Делает вывод без достаточных данных"),
    ("advice_instead_of_discovery", "Даёт совет вместо диагностики"),
    ("premature_diagnosis", "Слишком рано ставит диагноз"),
    ("premature_offer", "Слишком рано предлагает тест-драйв"),
    ("missed_offer", "Пропускает подходящий момент для оффера"),
    ("offer_to_non_target", "Предлагает тест-драйв нецелевому лиду"),
    ("misread_intent", "Неверно понимает согласие или сомнение"),
    ("pressure_or_promise", "Давит или обещает результат"),
    ("tone_or_length", "Неудачный тон или слишком длинный ответ"),
    ("off_topic", "Уходит от темы"),
    ("other", "Другое"),
)


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


def _render_choices(
    field: str,
    options: Sequence[tuple[str, str]],
    dialogue_index: int,
) -> str:
    return "".join(
        '<label class="choice"><input type="radio" '
        f'data-field="{field}" name="{field}-{dialogue_index}" '
        f'value="{escape(value, quote=True)}"><span>{escape(label)}</span></label>'
        for value, label in options
    )


def _render_review_form(dialogue_index: int) -> str:
    failure_tags = "".join(
        '<label class="choice"><input type="checkbox" name="failure_tags" '
        f'value="{escape(value, quote=True)}"><span>{escape(label)}</span></label>'
        for value, label in _FAILURE_TAGS
    )
    return f"""
      <section class="review-panel">
        <header class="review-heading">
          <h3>Оценка диалога</h3>
          <span class="dialogue-state" aria-live="polite">Не оценён</span>
        </header>
        <fieldset data-required-field="lead_status">
          <legend>Как вы оцениваете лида по итогам диалога?</legend>
          <div class="choices">{_render_choices("lead_status", _LEAD_OPTIONS, dialogue_index)}</div>
        </fieldset>
        <fieldset data-required-field="response_acceptable">
          <legend>Можно ли оставить ответы бота в этом диалоге без исправлений?</legend>
          <div class="choices">{_render_choices("response_acceptable", _RESPONSE_OPTIONS, dialogue_index)}</div>
        </fieldset>
        <fieldset data-required-field="button_should_be_shown_now">
          <legend>Должна ли к концу диалога появиться кнопка тест-драйва?</legend>
          <div class="choices">{_render_choices("button_should_be_shown_now", _BUTTON_OPTIONS, dialogue_index)}</div>
        </fieldset>
        <fieldset class="failure-section">
          <legend>Если ответы плохие, что именно сломано?</legend>
          <div class="choices tags">{failure_tags}</div>
        </fieldset>
        <label class="textarea-label failure-section">
          Что бот должен был сделать вместо этого?
          <span>необязательно; достаточно следующего шага в 1–2 предложениях</span>
          <textarea name="expected_behavior" rows="3"></textarea>
        </label>
        <label class="textarea-label failure-section">
          Пример более удачного ответа <span>необязательно</span>
          <textarea name="suggested_response" rows="3"></textarea>
        </label>
        <label class="textarea-label">
          Комментарий эксперта <span>необязательно</span>
          <textarea name="expert_note" rows="2"></textarea>
        </label>
      </section>
    """


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
        user_record_id = escape(str(dialogue.get("user_record_id") or ""), quote=True)
        dialogue_id = escape(
            f"dialogue-{dialogue.get('user_record_id') or index}", quote=True
        )
        telegram_user_id = escape(
            str(dialogue.get("telegram_user_id") or ""), quote=True
        )
        sections.append(
            f"""
            <section class="dialogue" id="dialogue-{index}" data-dialogue-id="{dialogue_id}" data-user-record-id="{user_record_id}" data-telegram-user-id="{telegram_user_id}">
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
              {_render_review_form(index)}
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
    generated_iso = escape(generated_at.isoformat(), quote=True)
    report_id = escape(
        f"dialogues-{generated_at.strftime('%Y-%m-%d_%H-%M-%S')}", quote=True
    )
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
    main {{ width: min(100% - 28px, 980px); margin: 24px auto 120px; }}
    .review-intro {{ margin: 0 0 28px; padding: 18px; border: 1px solid #cbd7de; border-radius: 14px; background: var(--panel); box-shadow: 0 5px 22px #3449551f; }}
    .review-intro h2 {{ margin: 0 0 6px; font-size: 18px; }}
    .review-intro p {{ margin: 0; color: var(--muted); }}
    .reviewer {{ display: grid; gap: 7px; max-width: 440px; margin-top: 15px; font-weight: 650; }}
    input[type=text], textarea {{ width: 100%; padding: 11px 12px; border: 1px solid #bdcbc5; border-radius: 10px; color: inherit; background: #fff; font: inherit; }}
    input[type=text]:focus, textarea:focus {{ outline: 3px solid #3390ec24; border-color: var(--accent); }}
    .status {{ min-height: 22px; margin: 10px 0 0; color: #ae3d35; font-weight: 650; }}
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
    .review-panel {{ padding: 20px 22px 24px; border-top: 1px solid var(--line); background: #fff; }}
    .review-heading {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
    .review-heading h3 {{ margin: 0; font-size: 18px; }}
    .dialogue-state {{ padding: 5px 10px; border-radius: 999px; color: var(--muted); background: #eef2f0; font-size: 13px; }}
    .dialogue-state.done {{ color: #056348; background: #e7f5ef; }}
    .dialogue.incomplete {{ border-color: #e3a09a; box-shadow: 0 0 0 3px #ae3d3514; }}
    fieldset {{ margin: 21px 0 0; padding: 0; border: 0; }}
    legend, .textarea-label {{ font-weight: 700; }}
    .choices {{ display: flex; flex-wrap: wrap; gap: 9px; margin-top: 10px; }}
    .choice {{ position: relative; display: flex; align-items: center; cursor: pointer; }}
    .choice input {{ position: absolute; opacity: 0; pointer-events: none; }}
    .choice span {{ display: block; padding: 9px 12px; border: 1px solid #c7d3ce; border-radius: 9px; background: #fff; font-size: 14px; font-weight: 550; }}
    .choice input:checked + span {{ border-color: #5cab91; color: #055d44; background: #e7f5ef; box-shadow: 0 0 0 2px #087d5b1a; }}
    .choice input:focus-visible + span {{ outline: 3px solid #087d5b33; }}
    .tags .choice span {{ font-weight: 450; }}
    .failure-section {{ display: none; }}
    .dialogue.is-rejected .failure-section {{ display: block; }}
    .textarea-label {{ display: block; margin-top: 21px; }}
    .textarea-label > span {{ color: var(--muted); font-size: 13px; font-weight: 400; }}
    textarea {{ display: block; margin-top: 8px; resize: vertical; }}
    .toolbar {{ position: fixed; z-index: 20; right: 0; bottom: 0; left: 0; padding: 13px 16px; border-top: 1px solid var(--line); background: #fffffff2; backdrop-filter: blur(12px); }}
    .toolbar-inner {{ display: flex; align-items: center; gap: 12px; width: min(980px, 100%); margin: auto; }}
    .progress {{ flex: 1; min-width: 140px; }}
    .progress strong {{ display: block; }}
    .progress span {{ color: var(--muted); font-size: 13px; }}
    button {{ padding: 11px 15px; border: 0; border-radius: 10px; font: inherit; font-weight: 700; cursor: pointer; }}
    .download {{ color: #fff; background: var(--accent); }}
    .clear {{ color: var(--muted); background: #edf2ef; }}
    @media (max-width: 640px) {{
      .report-header {{ padding: 14px 16px; }} main {{ width: 100%; margin-top: 12px; }} .dialogue {{ border-radius: 0; border-left: 0; border-right: 0; }}
      .review-intro, .unanswered {{ border-radius: 0; border-left: 0; border-right: 0; }}
      .chat-header {{ align-items: flex-start; padding: 12px; }} .chat-stats {{ display: none; }} .bubble {{ max-width: 88%; }} .review-panel {{ padding: 18px 14px 22px; }}
      .toolbar-inner {{ flex-wrap: wrap; }} .progress {{ flex-basis: 100%; }} button {{ flex: 1; }}
    }}
    @media print {{ .report-header {{ position: static; }} main {{ width: 100%; margin: 12px 0; }} .dialogue {{ box-shadow: none; }} .toolbar, .reviewer, .status {{ display: none; }} }}
  </style>
</head>
<body data-report-id="{report_id}" data-generated-at="{generated_iso}">
  <header class="report-header">
    <h1>Все диалоги</h1>
    <p>{len(dialogues)} диалогов · {message_count} сообщений · без ответа {len(unanswered_users)} · сформирован {generated_label} MSK · от старых к новым</p>
  </header>
  <main>
    <section class="review-intro">
      <h2>Экспертная оценка</h2>
      <p>Оцените каждый диалог. Ответы сохраняются только в этом браузере и не отправляются в интернет.</p>
      <label class="reviewer">Ваше имя или идентификатор
        <input id="reviewer-id" type="text" autocomplete="name" placeholder="Например: client-expert-1">
      </label>
      <p class="status" id="status" role="alert"></p>
    </section>
    {unanswered_content}{content}
  </main>
  <footer class="toolbar">
    <div class="toolbar-inner">
      <div class="progress"><strong id="progress-label">Оценено 0 из {len(dialogues)}</strong><span>Ответы сохраняются автоматически</span></div>
      <button class="clear" id="clear" type="button">Очистить</button>
      <button class="download" id="download" type="button">Скачать reviewed.json</button>
    </div>
  </footer>
  <script>
    (() => {{
      "use strict";
      const schemaVersion = "dialogue-review-v1";
      const reportId = document.body.dataset.reportId;
      const generatedAt = document.body.dataset.generatedAt;
      const storageKey = "siemensbot-dialogue-review:" + reportId;
      const dialogues = Array.from(document.querySelectorAll(".dialogue"));
      const reviewer = document.getElementById("reviewer-id");
      const status = document.getElementById("status");
      const progress = document.getElementById("progress-label");

      function selected(dialogue, name) {{
        const input = dialogue.querySelector(`input[data-field="${{name}}"]:checked`);
        return input ? input.value : "";
      }}

      function collectReview(dialogue) {{
        return {{
          dialogue_id: dialogue.dataset.dialogueId,
          user_record_id: dialogue.dataset.userRecordId,
          telegram_user_id: dialogue.dataset.telegramUserId,
          lead_status: selected(dialogue, "lead_status"),
          response_acceptable: selected(dialogue, "response_acceptable"),
          button_should_be_shown_now: selected(dialogue, "button_should_be_shown_now"),
          failure_tags: Array.from(dialogue.querySelectorAll('input[name="failure_tags"]:checked')).map((node) => node.value),
          expected_behavior: dialogue.querySelector('[name="expected_behavior"]').value.trim(),
          suggested_response: dialogue.querySelector('[name="suggested_response"]').value.trim(),
          expert_note: dialogue.querySelector('[name="expert_note"]').value.trim()
        }};
      }}

      function isComplete(review) {{
        return Boolean(review.lead_status && review.response_acceptable && review.button_should_be_shown_now);
      }}

      function snapshot() {{
        return {{ reviewer_id: reviewer.value.trim(), reviews: dialogues.map(collectReview) }};
      }}

      function updateUi() {{
        let complete = 0;
        dialogues.forEach((dialogue) => {{
          const review = collectReview(dialogue);
          const done = isComplete(review);
          complete += done ? 1 : 0;
          dialogue.classList.toggle("is-rejected", review.response_acceptable === "no");
          const badge = dialogue.querySelector(".dialogue-state");
          badge.textContent = done ? "Оценён" : "Не оценён";
          badge.classList.toggle("done", done);
        }});
        progress.textContent = `Оценено ${{complete}} из ${{dialogues.length}}`;
      }}

      function save() {{
        updateUi();
        try {{
          localStorage.setItem(storageKey, JSON.stringify(snapshot()));
          status.textContent = "";
        }} catch (error) {{
          status.textContent = "Браузер не разрешил локальное сохранение. Не закрывайте страницу до скачивания JSON.";
        }}
      }}

      function restore() {{
        let saved;
        try {{ saved = JSON.parse(localStorage.getItem(storageKey) || "null"); }} catch (error) {{ saved = null; }}
        if (!saved || !Array.isArray(saved.reviews)) return;
        reviewer.value = saved.reviewer_id || "";
        const byId = new Map(saved.reviews.map((item) => [item.dialogue_id, item]));
        dialogues.forEach((dialogue) => {{
          const item = byId.get(dialogue.dataset.dialogueId);
          if (!item) return;
          ["lead_status", "response_acceptable", "button_should_be_shown_now"].forEach((name) => {{
            const input = Array.from(dialogue.querySelectorAll(`input[data-field="${{name}}"]`)).find((node) => node.value === item[name]);
            if (input) input.checked = true;
          }});
          const tags = new Set(Array.isArray(item.failure_tags) ? item.failure_tags : []);
          dialogue.querySelectorAll('input[name="failure_tags"]').forEach((node) => {{ node.checked = tags.has(node.value); }});
          ["expected_behavior", "suggested_response", "expert_note"].forEach((name) => {{
            dialogue.querySelector(`[name="${{name}}"]`).value = item[name] || "";
          }});
        }});
      }}

      document.addEventListener("input", save);
      document.addEventListener("change", save);
      document.getElementById("clear").addEventListener("click", () => {{
        if (!window.confirm("Удалить всю разметку этого отчёта из браузера?")) return;
        localStorage.removeItem(storageKey);
        reviewer.value = "";
        dialogues.forEach((dialogue) => dialogue.querySelectorAll("input, textarea").forEach((node) => {{
          if (node.type === "radio" || node.type === "checkbox") node.checked = false;
          else node.value = "";
        }}));
        status.textContent = "";
        updateUi();
      }});

      document.getElementById("download").addEventListener("click", () => {{
        const data = snapshot();
        dialogues.forEach((dialogue) => dialogue.classList.remove("incomplete"));
        if (!data.reviewer_id) {{
          status.textContent = "Укажите имя или идентификатор эксперта.";
          reviewer.focus();
          return;
        }}
        const incomplete = dialogues.filter((dialogue, index) => {{
          const missing = !isComplete(data.reviews[index]);
          dialogue.classList.toggle("incomplete", missing);
          return missing;
        }});
        const artifact = {{
          schema_version: schemaVersion,
          report_id: reportId,
          generated_at: generatedAt,
          reviewer_id: data.reviewer_id,
          reviews: data.reviews
        }};
        const blob = new Blob([JSON.stringify(artifact, null, 2) + "\\n"], {{ type: "application/json;charset=utf-8" }});
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = `reviewed-${{reportId.replace(/[^a-zA-Z0-9._-]+/g, "-")}}.json`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
        status.textContent = incomplete.length
          ? `Скачан частично заполненный JSON. Не заполнено: ${{incomplete.length}} из ${{dialogues.length}} диалогов.`
          : "JSON скачан.";
      }});

      restore();
      updateUi();
    }})();
  </script>
</body>
</html>
"""
    return document.encode("utf-8")
