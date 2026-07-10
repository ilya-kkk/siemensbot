"""Local, dependency-free reports and human-feedback artifacts for sales evals.

The business report is deliberately blind: it contains only the conversation,
the latest user message, the assistant response, and whether a button was
rendered. Expected behavior, hard checks, judge feedback, and model names are
only present in the technical report.
"""

from __future__ import annotations

import copy
import html
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

RUN_SCHEMA_VERSION = "sales-eval-run-v1"
HUMAN_REVIEW_SCHEMA_VERSION = "human-review-v1"
ALIGNMENT_SCHEMA_VERSION = "human-judge-alignment-v1"
PROMPT_PACKET_SCHEMA_VERSION = "prompt-improvement-packet-v1"

LEAD_STATUSES = {"target", "non_target", "not_enough_data", "unsure"}
RESPONSE_VERDICTS = {"yes", "no", "unsure"}
BUTTON_VERDICTS = {"yes", "no", "need_more_qualification", "unsure"}

FAILURE_TAGS: tuple[tuple[str, str], ...] = (
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
FAILURE_TAG_VALUES = {value for value, _label in FAILURE_TAGS}


def _require_run(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(run, Mapping):
        raise TypeError("run must be a mapping")
    if run.get("schema_version") != RUN_SCHEMA_VERSION:
        raise ValueError(f"run.schema_version must be {RUN_SCHEMA_VERSION!r}")
    if not isinstance(run.get("run_id"), str) or not run["run_id"].strip():
        raise ValueError("run.run_id must be a non-empty string")
    cases = run.get("cases")
    if not isinstance(cases, list):
        raise ValueError("run.cases must be a list")

    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"run.cases[{index}] must be an object")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"run.cases[{index}].case_id must be a non-empty string")
        if case_id in seen:
            raise ValueError(f"duplicate case_id in run: {case_id!r}")
        seen.add(case_id)
    return cases


def _text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    if isinstance(value, str):
        return value
    return str(value)


def _escaped(value: Any, fallback: str = "") -> str:
    return html.escape(_text(value, fallback), quote=True)


def _pre(value: Any, fallback: str = "—") -> str:
    return f'<pre class="message">{_escaped(value, fallback)}</pre>'


def _bool_label(value: Any) -> str:
    if value is True:
        return "Да"
    if value is False:
        return "Нет"
    return "Не указано"


def _number(value: Any) -> str:
    if isinstance(value, bool):
        return _bool_label(value)
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return _text(value, "—")


def _write_text(path: str | Path, content: str) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    return output


def write_json_artifact(data: Mapping[str, Any] | Sequence[Any], path: str | Path) -> Path:
    """Write a stable, human-readable JSON artifact."""

    return _write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def render_business_review_html(
    run: Mapping[str, Any],
    *,
    title: str = "Экспертная оценка диалогов",
) -> str:
    """Render a blind, self-contained HTML form for a business reviewer."""

    cases = _require_run(run)
    run_id = _text(run["run_id"])
    cards: list[str] = []

    lead_options = (
        ("target", "Целевой"),
        ("non_target", "Нецелевой"),
        ("not_enough_data", "Пока недостаточно данных"),
        ("unsure", "Не уверен"),
    )
    response_options = (
        ("yes", "Да"),
        ("no", "Нет"),
        ("unsure", "Не уверен"),
    )
    button_options = (
        ("yes", "Да"),
        ("no", "Нет"),
        ("need_more_qualification", "Сначала нужна квалификация"),
        ("unsure", "Не уверен"),
    )

    def radios(field: str, options: Sequence[tuple[str, str]], group_index: int) -> str:
        return "".join(
            '<label class="choice"><input type="radio" '
            f'data-field="{field}" name="{field}-{group_index}" '
            f'value="{html.escape(value, quote=True)}">'
            f"<span>{html.escape(label)}</span></label>"
            for value, label in options
        )

    tag_choices = "".join(
        '<label class="choice"><input type="checkbox" name="failure_tags" '
        f'value="{html.escape(value, quote=True)}"><span>{html.escape(label)}</span></label>'
        for value, label in FAILURE_TAGS
    )

    for index, case in enumerate(cases, start=1):
        output = case.get("assistant_output")
        output = output if isinstance(output, Mapping) else {}
        transcript = _text(case.get("transcript"), "Диалог начинается с этого сообщения.")
        user_message = _text(case.get("user_message"), "—")
        assistant_text = _text(output.get("text"), "—")
        button_rendered = _bool_label(output.get("button_rendered"))
        cards.append(
            f"""
            <article class="review-card" data-case-id="{_escaped(case["case_id"])}">
              <header class="case-header">
                <p class="eyebrow">Пример {index} из {len(cases)}</p>
                <span class="case-state" aria-live="polite">Не оценён</span>
              </header>
              <section class="context-block">
                <h2>Предыдущий диалог</h2>
                {_pre(transcript)}
                <h2>Новое сообщение пользователя</h2>
                {_pre(user_message)}
                <h2>Ответ бота</h2>
                {_pre(assistant_text)}
                <div class="button-fact"><span>Кнопка фактически показана</span><strong>{button_rendered}</strong></div>
              </section>

              <fieldset data-required-field="lead_status">
                <legend>Как вы оцениваете лида на этом этапе?</legend>
                <div class="choices">{radios("lead_status", lead_options, index)}</div>
              </fieldset>
              <fieldset data-required-field="response_acceptable">
                <legend>Можно ли отправить этот ответ реальному потенциальному клиенту без исправлений?</legend>
                <div class="choices">{radios("response_acceptable", response_options, index)}</div>
              </fieldset>
              <fieldset data-required-field="button_should_be_shown_now">
                <legend>Должна ли после этой реплики появиться кнопка тест-драйва?</legend>
                <div class="choices">{radios("button_should_be_shown_now", button_options, index)}</div>
              </fieldset>
              <fieldset class="failure-section">
                <legend>Если ответ плохой, что именно сломано?</legend>
                <div class="choices tags">{tag_choices}</div>
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
            </article>
            """
        )

    safe_title = html.escape(title)
    safe_run_id = html.escape(run_id, quote=True)
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <style>
    :root {{ color-scheme: light; --ink:#15231f; --muted:#66736e; --line:#dce5e1; --paper:#fff; --wash:#f4f7f5; --accent:#087d5b; --accent-soft:#e7f5ef; --danger:#ae3d35; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--wash); color:var(--ink); font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
    .shell {{ width:min(960px,calc(100% - 32px)); margin:32px auto 120px; }}
    .intro,.review-card {{ background:var(--paper); border:1px solid var(--line); border-radius:18px; box-shadow:0 8px 30px rgba(24,51,42,.05); }}
    .intro {{ padding:28px; margin-bottom:22px; }}
    h1 {{ margin:0 0 8px; font-size:clamp(27px,5vw,42px); line-height:1.1; letter-spacing:-.03em; }}
    .intro p {{ color:var(--muted); margin:8px 0 0; max-width:720px; }}
    .reviewer {{ display:grid; gap:7px; margin-top:22px; max-width:440px; font-weight:650; }}
    input[type=text],textarea {{ width:100%; border:1px solid #bdcbc5; border-radius:10px; padding:11px 12px; font:inherit; color:inherit; background:#fff; }}
    input[type=text]:focus,textarea:focus {{ outline:3px solid rgba(8,125,91,.14); border-color:var(--accent); }}
    .review-card {{ padding:26px; margin:20px 0; scroll-margin-top:18px; }}
    .review-card.incomplete {{ border-color:#e3a09a; box-shadow:0 0 0 3px rgba(174,61,53,.08); }}
    .case-header {{ display:flex; justify-content:space-between; align-items:center; gap:16px; margin-bottom:18px; }}
    .eyebrow {{ margin:0; text-transform:uppercase; letter-spacing:.09em; font-weight:750; color:var(--muted); font-size:12px; }}
    .case-state {{ padding:5px 10px; border-radius:99px; background:#eef2f0; color:var(--muted); font-size:13px; }}
    .case-state.done {{ background:var(--accent-soft); color:#056348; }}
    .context-block {{ padding:18px; background:#f8faf9; border:1px solid var(--line); border-radius:14px; }}
    h2 {{ margin:18px 0 7px; font-size:14px; color:var(--muted); }}
    h2:first-child {{ margin-top:0; }}
    .message {{ margin:0; padding:12px 14px; border-radius:10px; background:#fff; border:1px solid #e4ebe8; white-space:pre-wrap; overflow-wrap:anywhere; font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; }}
    .button-fact {{ display:flex; justify-content:space-between; gap:16px; margin-top:14px; padding-top:14px; border-top:1px solid var(--line); }}
    fieldset {{ margin:23px 0 0; padding:0; border:0; }}
    legend,.textarea-label {{ font-weight:700; }}
    .choices {{ display:flex; flex-wrap:wrap; gap:9px; margin-top:10px; }}
    .choice {{ position:relative; display:flex; align-items:center; cursor:pointer; }}
    .choice input {{ position:absolute; opacity:0; pointer-events:none; }}
    .choice span {{ display:block; padding:9px 12px; border:1px solid #c7d3ce; border-radius:9px; background:#fff; font-weight:550; font-size:14px; }}
    .choice input:checked + span {{ color:#055d44; background:var(--accent-soft); border-color:#5cab91; box-shadow:0 0 0 2px rgba(8,125,91,.1); }}
    .choice input:focus-visible + span {{ outline:3px solid rgba(8,125,91,.2); }}
    .tags .choice span {{ font-weight:450; }}
    .failure-section {{ display:none; }}
    .review-card.is-rejected .failure-section {{ display:block; }}
    .textarea-label {{ display:block; margin-top:22px; }}
    .textarea-label > span {{ color:var(--muted); font-weight:400; font-size:13px; }}
    textarea {{ display:block; margin-top:8px; resize:vertical; }}
    .toolbar {{ position:fixed; z-index:5; left:0; right:0; bottom:0; padding:13px 16px; background:rgba(255,255,255,.95); border-top:1px solid var(--line); backdrop-filter:blur(12px); }}
    .toolbar-inner {{ width:min(960px,100%); margin:auto; display:flex; align-items:center; gap:12px; }}
    .progress {{ flex:1; min-width:140px; }}
    .progress strong {{ display:block; }}
    .progress span {{ color:var(--muted); font-size:13px; }}
    button {{ border:0; border-radius:10px; padding:11px 15px; font:inherit; font-weight:700; cursor:pointer; }}
    .download {{ background:var(--accent); color:#fff; }}
    .clear {{ color:var(--muted); background:#edf2ef; }}
    .status {{ min-height:24px; margin:10px 0 0; color:var(--danger); font-weight:650; }}
    @media (max-width:680px) {{ .shell {{ width:min(100% - 18px,960px); margin-top:10px; }} .intro,.review-card {{ padding:18px; border-radius:13px; }} .toolbar-inner {{ flex-wrap:wrap; }} .progress {{ flex-basis:100%; }} button {{ flex:1; }} }}
    @media print {{ .toolbar,.reviewer,.status {{ display:none; }} body {{ background:#fff; }} .shell {{ width:100%; margin:0; }} .review-card,.intro {{ box-shadow:none; break-inside:avoid; }} }}
  </style>
</head>
<body data-run-id="{safe_run_id}">
  <main class="shell">
    <section class="intro">
      <h1>{safe_title}</h1>
      <p>Оцените каждый ответ независимо. Результаты сохраняются только в этом браузере и не отправляются в интернет.</p>
      <label class="reviewer">Ваше имя или идентификатор
        <input id="reviewer-id" type="text" autocomplete="name" placeholder="Например: client-expert-1">
      </label>
      <p class="status" id="status" role="alert"></p>
    </section>
    {"".join(cards)}
  </main>
  <footer class="toolbar">
    <div class="toolbar-inner">
      <div class="progress"><strong id="progress-label">Оценено 0 из {len(cases)}</strong><span>Ответы сохраняются автоматически</span></div>
      <button class="clear" id="clear" type="button">Очистить</button>
      <button class="download" id="download" type="button">Скачать reviewed.json</button>
    </div>
  </footer>
  <script>
    (() => {{
      "use strict";
      const schemaVersion = {json.dumps(HUMAN_REVIEW_SCHEMA_VERSION)};
      const runId = document.body.dataset.runId;
      const storageKey = "siemensbot-review:" + runId;
      const cards = Array.from(document.querySelectorAll(".review-card"));
      const reviewer = document.getElementById("reviewer-id");
      const status = document.getElementById("status");
      const progress = document.getElementById("progress-label");

      function selected(card, name) {{
        const input = card.querySelector(`input[data-field="${{name}}"]:checked`);
        return input ? input.value : "";
      }}

      function collectReview(card) {{
        return {{
          case_id: card.dataset.caseId,
          lead_status: selected(card, "lead_status"),
          response_acceptable: selected(card, "response_acceptable"),
          button_should_be_shown_now: selected(card, "button_should_be_shown_now"),
          failure_tags: Array.from(card.querySelectorAll('input[name="failure_tags"]:checked')).map((node) => node.value),
          expected_behavior: card.querySelector('[name="expected_behavior"]').value.trim(),
          suggested_response: card.querySelector('[name="suggested_response"]').value.trim(),
          expert_note: card.querySelector('[name="expert_note"]').value.trim()
        }};
      }}

      function isComplete(review) {{
        return Boolean(review.lead_status && review.response_acceptable && review.button_should_be_shown_now);
      }}

      function snapshot() {{
        return {{ reviewer_id: reviewer.value.trim(), reviews: cards.map(collectReview) }};
      }}

      function updateUi() {{
        let complete = 0;
        cards.forEach((card) => {{
          const review = collectReview(card);
          const done = isComplete(review);
          complete += done ? 1 : 0;
          card.classList.toggle("is-rejected", review.response_acceptable === "no");
          const badge = card.querySelector(".case-state");
          badge.textContent = done ? "Оценён" : "Не оценён";
          badge.classList.toggle("done", done);
        }});
        progress.textContent = `Оценено ${{complete}} из ${{cards.length}}`;
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
        const byId = new Map(saved.reviews.map((item) => [item.case_id, item]));
        cards.forEach((card) => {{
          const item = byId.get(card.dataset.caseId);
          if (!item) return;
          ["lead_status", "response_acceptable", "button_should_be_shown_now"].forEach((name) => {{
            const value = item[name];
            const input = Array.from(card.querySelectorAll(`input[data-field="${{name}}"]`)).find((node) => node.value === value);
            if (input) input.checked = true;
          }});
          const tags = new Set(Array.isArray(item.failure_tags) ? item.failure_tags : []);
          card.querySelectorAll('input[name="failure_tags"]').forEach((node) => {{ node.checked = tags.has(node.value); }});
          ["expected_behavior", "suggested_response", "expert_note"].forEach((name) => {{
            card.querySelector(`[name="${{name}}"]`).value = item[name] || "";
          }});
        }});
      }}

      document.addEventListener("input", save);
      document.addEventListener("change", save);
      document.getElementById("clear").addEventListener("click", () => {{
        if (!window.confirm("Удалить всю разметку этого прогона из браузера?")) return;
        localStorage.removeItem(storageKey);
        reviewer.value = "";
        cards.forEach((card) => card.querySelectorAll("input, textarea").forEach((node) => {{
          if (node.type === "radio" || node.type === "checkbox") node.checked = false;
          else node.value = "";
        }}));
        updateUi();
      }});

      document.getElementById("download").addEventListener("click", () => {{
        const data = snapshot();
        cards.forEach((card) => card.classList.remove("incomplete"));
        if (!data.reviewer_id) {{ status.textContent = "Укажите имя или идентификатор эксперта."; reviewer.focus(); return; }}
        const firstIncompleteIndex = data.reviews.findIndex((item) => !isComplete(item));
        if (firstIncompleteIndex !== -1) {{
          const card = cards[firstIncompleteIndex];
          card.classList.add("incomplete");
          card.scrollIntoView({{ behavior:"smooth", block:"start" }});
          status.textContent = `Заполните три обязательных ответа в примере ${{firstIncompleteIndex + 1}}.`;
          return;
        }}
        const artifact = {{ schema_version:schemaVersion, run_id:runId, reviewer_id:data.reviewer_id, reviews:data.reviews }};
        const blob = new Blob([JSON.stringify(artifact, null, 2) + "\\n"], {{ type:"application/json;charset=utf-8" }});
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = `reviewed-${{runId.replace(/[^a-zA-Z0-9._-]+/g, "-")}}.json`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
        status.textContent = "JSON скачан. Отправьте этот файл команде проекта.";
      }});

      restore();
      updateUi();
    }})();
  </script>
</body>
</html>
"""


def write_business_review_html(
    run: Mapping[str, Any],
    path: str | Path,
    *,
    title: str = "Экспертная оценка диалогов",
) -> Path:
    return _write_text(path, render_business_review_html(run, title=title))


def _summary_stats(run: Mapping[str, Any], cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    hard_checks = [
        check
        for case in cases
        for check in case.get("hard_checks", [])
        if isinstance(check, Mapping)
    ]
    judge_metrics = [
        metric
        for case in cases
        for metric in case.get("judge_metrics", [])
        if isinstance(metric, Mapping)
    ]
    scores = [
        float(metric["score"])
        for metric in judge_metrics
        if isinstance(metric.get("score"), int | float)
        and not isinstance(metric.get("score"), bool)
    ]
    return {
        "Сценариев": len(cases),
        "Сценариев с показанной кнопкой": sum(
            (case.get("assistant_output") or {}).get("button_rendered") is True
            for case in cases
            if isinstance(case.get("assistant_output"), Mapping)
        ),
        "Hard checks: ошибок": sum(check.get("passed") is False for check in hard_checks),
        "Judge metrics: ошибок": sum(metric.get("passed") is False for metric in judge_metrics),
        "Средний judge score": sum(scores) / len(scores) if scores else None,
    }


def _table(rows: Sequence[tuple[Any, Any]], *, css_class: str = "kv") -> str:
    if not rows:
        return '<p class="empty">Нет данных</p>'
    body = "".join(
        f"<tr><th>{_escaped(key)}</th><td>{_escaped(_number(value), '—')}</td></tr>"
        for key, value in rows
    )
    return f'<table class="{html.escape(css_class, quote=True)}"><tbody>{body}</tbody></table>'


def _human_review_html(review: Mapping[str, Any] | None) -> str:
    if not review:
        return '<p class="empty">Экспертная разметка ещё не добавлена.</p>'
    tags = review.get("failure_tags")
    tag_text = ", ".join(map(str, tags)) if isinstance(tags, list) and tags else "—"
    return _table(
        [
            ("Эксперт", review.get("reviewer_id")),
            ("Статус лида", review.get("lead_status")),
            ("Ответ приемлем", review.get("response_acceptable")),
            ("Нужна кнопка сейчас", review.get("button_should_be_shown_now")),
            ("Теги ошибок", tag_text),
            ("Ожидаемое поведение", review.get("expected_behavior")),
            ("Предложенный ответ", review.get("suggested_response")),
            ("Комментарий", review.get("expert_note")),
        ]
    )


def render_technical_report_html(
    run: Mapping[str, Any],
    *,
    title: str = "Технический отчёт DeepEval",
) -> str:
    """Render a self-contained report with expectations, checks, and judge reasons."""

    cases = _require_run(run)
    supplied_summary = run.get("summary") if isinstance(run.get("summary"), Mapping) else {}
    computed = _summary_stats(run, cases)
    prompt = run.get("prompt") if isinstance(run.get("prompt"), Mapping) else {}
    overview_rows: list[tuple[Any, Any]] = [
        ("Run ID", run.get("run_id")),
        ("Создан", run.get("generated_at")),
        ("Dataset", run.get("dataset_version")),
        ("Target model", run.get("target_model")),
        ("Judge model", run.get("judge_model")),
        ("Prompt path", prompt.get("path")),
        ("Prompt SHA-256", prompt.get("sha256")),
    ]

    summary_rows = list(computed.items())
    summary_rows.extend((f"run.summary.{key}", value) for key, value in supplied_summary.items())
    cards: list[str] = []
    for case in cases:
        expected = case.get("expected") if isinstance(case.get("expected"), Mapping) else {}
        output = (
            case.get("assistant_output")
            if isinstance(case.get("assistant_output"), Mapping)
            else {}
        )
        hard_checks = [item for item in case.get("hard_checks", []) if isinstance(item, Mapping)]
        judge_metrics = [
            item for item in case.get("judge_metrics", []) if isinstance(item, Mapping)
        ]
        failures = sum(item.get("passed") is False for item in hard_checks + judge_metrics)
        status_class = "bad" if failures else "good"
        status_text = f"{failures} ошибок" if failures else "Пройден"
        must_not = expected.get("must_not")
        if isinstance(must_not, list):
            must_not_text = "\n".join(f"• {item}" for item in must_not) or "—"
        else:
            must_not_text = _text(must_not, "—")

        hard_rows = (
            "".join(
                "<tr>"
                f"<td>{_escaped(check.get('name'), '—')}</td>"
                f'<td><span class="pill {"pass" if check.get("passed") is True else "fail"}">{_bool_label(check.get("passed"))}</span></td>'
                f"<td>{_escaped(check.get('expected'), '—')}</td>"
                f"<td>{_escaped(check.get('actual'), '—')}</td>"
                f"<td>{_escaped(check.get('reason'), '—')}</td>"
                "</tr>"
                for check in hard_checks
            )
            or '<tr><td colspan="5" class="empty">Нет hard checks</td></tr>'
        )

        judge_rows = (
            "".join(
                "<tr>"
                f"<td>{_escaped(metric.get('name'), '—')}</td>"
                f"<td>{_escaped(_number(metric.get('score')), '—')}</td>"
                f"<td>{_escaped(_number(metric.get('threshold')), '—')}</td>"
                f'<td><span class="pill {"pass" if metric.get("passed") is True else "fail"}">{_bool_label(metric.get("passed"))}</span></td>'
                f"<td>{_escaped(metric.get('reason'), '—')}</td>"
                f"<td>{_escaped(metric.get('error'), '—')}</td>"
                "</tr>"
                for metric in judge_metrics
            )
            or '<tr><td colspan="6" class="empty">Нет judge metrics</td></tr>'
        )

        cards.append(
            f"""
            <details class="case" open data-status="{status_class}">
              <summary>
                <span><strong>{_escaped(case.get("case_id"))}</strong><small>{
                _escaped(case.get("family"), "—")
            } · {_escaped(case.get("scope"), "—")}</small></span>
                <span class="status {status_class}">{status_text}</span>
              </summary>
              <div class="case-body">
                <div class="columns">
                  <section><h3>Предыдущий диалог</h3>{_pre(case.get("transcript"))}</section>
                  <section><h3>Новое сообщение</h3>{_pre(case.get("user_message"))}</section>
                </div>
                <section><h3>Ответ ассистента</h3>{_pre(output.get("text"))}</section>
                <div class="columns compact">
                  <section><h3>Фактический результат</h3>{
                _table(
                    [
                        ("should_send_offer", output.get("should_send_offer")),
                        ("button_rendered", output.get("button_rendered")),
                        ("source", output.get("source")),
                    ]
                )
            }</section>
                  <section><h3>Ожидание сценария</h3>{
                _table(
                    [
                        ("lead_status", expected.get("lead_status")),
                        ("button_action", expected.get("button_action")),
                        ("behavior", expected.get("behavior")),
                        ("must_not", must_not_text),
                    ]
                )
            }</section>
                </div>
                <section><h3>Hard checks</h3><div class="table-scroll"><table><thead><tr><th>Проверка</th><th>Pass</th><th>Expected</th><th>Actual</th><th>Причина</th></tr></thead><tbody>{
                hard_rows
            }</tbody></table></div></section>
                <section><h3>Judge metrics</h3><div class="table-scroll"><table><thead><tr><th>Метрика</th><th>Score</th><th>Порог</th><th>Pass</th><th>Причина judge</th><th>Ошибка</th></tr></thead><tbody>{
                judge_rows
            }</tbody></table></div></section>
                <section><h3>Оценка заказчика</h3>{
                _human_review_html(
                    case.get("human_review")
                    if isinstance(case.get("human_review"), Mapping)
                    else None
                )
            }</section>
              </div>
            </details>
            """
        )

    alignment = run.get("alignment") if isinstance(run.get("alignment"), Mapping) else None
    alignment_html = ""
    if alignment:
        response = (
            alignment.get("response") if isinstance(alignment.get("response"), Mapping) else {}
        )
        button = alignment.get("button") if isinstance(alignment.get("button"), Mapping) else {}
        lead = (
            alignment.get("lead_status")
            if isinstance(alignment.get("lead_status"), Mapping)
            else {}
        )
        alignment_html = f"""
          <section class="panel">
            <h2>Совпадение human ↔ judge / dataset</h2>
            <div class="columns compact">
              <div>{_table([("Response compared", response.get("compared")), ("Response agreement", response.get("agreement_rate")), ("Response disagreements", len(response.get("disagreements", [])))])}</div>
              <div>{_table([("Button compared", button.get("compared")), ("Button agreement", button.get("agreement_rate")), ("Lead-status compared", lead.get("compared")), ("Lead-status agreement", lead.get("agreement_rate"))])}</div>
            </div>
          </section>
        """

    safe_title = html.escape(title)
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <style>
    :root {{ color-scheme:light; --ink:#17211f; --muted:#68736f; --line:#dce2df; --paper:#fff; --wash:#f3f5f4; --good:#147453; --bad:#ad3f38; }}
    * {{ box-sizing:border-box; }} body {{ margin:0; background:var(--wash); color:var(--ink); font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
    main {{ width:min(1280px,calc(100% - 32px)); margin:30px auto 80px; }}
    h1 {{ margin:0; font-size:36px; letter-spacing:-.03em; }} h2 {{ margin:0 0 14px; }} h3 {{ margin:22px 0 8px; font-size:14px; color:#47534f; }}
    .subtitle {{ margin:5px 0 22px; color:var(--muted); }} .panel,.case {{ background:var(--paper); border:1px solid var(--line); border-radius:14px; box-shadow:0 7px 25px rgba(31,45,40,.04); }}
    .panel {{ padding:22px; margin:16px 0; }} .columns {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }} .columns.compact {{ align-items:start; }}
    table {{ width:100%; border-collapse:collapse; }} th,td {{ padding:9px 10px; border-bottom:1px solid #e7ebe9; text-align:left; vertical-align:top; overflow-wrap:break-word; word-break:normal; white-space:normal; }} th {{ color:#53605b; font-size:12px; }} .kv th {{ width:220px; }}
    .case {{ margin:14px 0; overflow:hidden; }} summary {{ cursor:pointer; display:flex; justify-content:space-between; align-items:center; gap:16px; padding:16px 20px; }} summary small {{ display:block; margin-top:2px; color:var(--muted); font-weight:400; }}
    .case-body {{ padding:0 20px 22px; border-top:1px solid var(--line); }} .status,.pill {{ display:inline-block; padding:4px 9px; border-radius:99px; font-size:12px; font-weight:700; }} .status.good,.pill.pass {{ color:#0b6448; background:#e4f4ed; }} .status.bad,.pill.fail {{ color:#91352f; background:#fae9e7; }}
    .message {{ margin:0; padding:12px; border:1px solid var(--line); border-radius:9px; background:#fafbfa; white-space:pre-wrap; overflow-wrap:anywhere; font:14px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace; }} .table-scroll {{ overflow-x:auto; }} .table-scroll table {{ min-width: 880px; }} .empty {{ color:var(--muted); }}
    @media (max-width:760px) {{ main {{ width:min(100% - 16px,1280px); margin-top:14px; }} .columns {{ grid-template-columns:1fr; }} .panel {{ padding:15px; }} summary,.case-body {{ padding-left:14px; padding-right:14px; }} .table-scroll table {{ min-width: 760px; }} }}
    @media print {{ body {{ background:#fff; }} main {{ width:100%; margin:0; }} .panel,.case {{ box-shadow:none; break-inside:avoid; }} details {{ display:block; }} }}
  </style>
</head>
<body>
  <main>
    <h1>{safe_title}</h1>
    <p class="subtitle">Полный внутренний отчёт: результаты генерации, точные проверки, оценки judge и человеческая разметка.</p>
    <section class="panel"><h2>Эксперимент</h2>{_table(overview_rows)}</section>
    <section class="panel"><h2>Сводка</h2>{_table(summary_rows)}</section>
    {alignment_html}
    <section><h2>Сценарии</h2>{"".join(cards)}</section>
  </main>
</body>
</html>
"""


def write_technical_report_html(
    run: Mapping[str, Any],
    path: str | Path,
    *,
    title: str = "Технический отчёт DeepEval",
) -> Path:
    return _write_text(path, render_technical_report_html(run, title=title))


def _validate_review(review: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(review, Mapping):
        raise TypeError("review must be a mapping")
    if review.get("schema_version") != HUMAN_REVIEW_SCHEMA_VERSION:
        raise ValueError(f"review.schema_version must be {HUMAN_REVIEW_SCHEMA_VERSION!r}")
    if not isinstance(review.get("run_id"), str) or not review["run_id"].strip():
        raise ValueError("review.run_id must be a non-empty string")
    if not isinstance(review.get("reviewer_id"), str) or not review["reviewer_id"].strip():
        raise ValueError("review.reviewer_id must be a non-empty string")
    reviews = review.get("reviews")
    if not isinstance(reviews, list):
        raise ValueError("review.reviews must be a list")

    seen: set[str] = set()
    for index, item in enumerate(reviews):
        prefix = f"review.reviews[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{prefix} must be an object")
        case_id = item.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"{prefix}.case_id must be a non-empty string")
        if case_id in seen:
            raise ValueError(f"duplicate case_id in review: {case_id!r}")
        seen.add(case_id)
        if item.get("lead_status") not in LEAD_STATUSES:
            raise ValueError(f"{prefix}.lead_status has an unsupported value")
        if item.get("response_acceptable") not in RESPONSE_VERDICTS:
            raise ValueError(f"{prefix}.response_acceptable has an unsupported value")
        if item.get("button_should_be_shown_now") not in BUTTON_VERDICTS:
            raise ValueError(f"{prefix}.button_should_be_shown_now has an unsupported value")
        tags = item.get("failure_tags")
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise ValueError(f"{prefix}.failure_tags must be a list of strings")
        unsupported_tags = sorted(set(tags) - FAILURE_TAG_VALUES)
        if unsupported_tags:
            raise ValueError(
                f"{prefix}.failure_tags contains unsupported values: "
                f"{', '.join(unsupported_tags)}"
            )
        for field in ("expected_behavior", "suggested_response", "expert_note"):
            if not isinstance(item.get(field), str):
                raise ValueError(f"{prefix}.{field} must be a string")
    return reviews


def merge_human_feedback(run: Mapping[str, Any], review: Mapping[str, Any]) -> dict[str, Any]:
    """Merge returned business feedback into a copy of a run by stable case_id."""

    cases = _require_run(run)
    reviews = _validate_review(review)
    if run["run_id"] != review["run_id"]:
        raise ValueError(f"run_id mismatch: run={run['run_id']!r}, review={review['run_id']!r}")

    run_ids = {case["case_id"] for case in cases}
    unknown = sorted(item["case_id"] for item in reviews if item["case_id"] not in run_ids)
    if unknown:
        raise ValueError(f"review contains unknown case_id values: {', '.join(unknown)}")

    merged = copy.deepcopy(dict(run))
    by_id = {item["case_id"]: copy.deepcopy(item) for item in reviews}
    reviewer_id = review["reviewer_id"].strip()
    for case in merged["cases"]:
        human_review = by_id.get(case["case_id"])
        if human_review is not None:
            human_review["reviewer_id"] = reviewer_id
            case["human_review"] = human_review

    merged["human_feedback"] = {
        "schema_version": HUMAN_REVIEW_SCHEMA_VERSION,
        "reviewer_id": reviewer_id,
        "reviewed_cases": len(reviews),
        "total_cases": len(cases),
    }
    merged["alignment"] = build_alignment_report(merged)
    return merged


def _case_judge_pass(case: Mapping[str, Any], metric_name: str | None) -> bool | None:
    metrics = [
        metric
        for metric in case.get("judge_metrics", [])
        if isinstance(metric, Mapping)
        and (metric_name is None or metric.get("name") == metric_name)
        and isinstance(metric.get("passed"), bool)
    ]
    if not metrics:
        return None
    return all(metric["passed"] for metric in metrics)


def _rate(agreements: int, compared: int) -> float | None:
    return agreements / compared if compared else None


def build_alignment_report(
    merged_run: Mapping[str, Any], *, judge_metric_name: str | None = None
) -> dict[str, Any]:
    """Compare human labels to judge verdicts and authored button/lead expectations."""

    cases = _require_run(merged_run)
    confusion = {
        "human_good_judge_good": 0,
        "human_good_judge_bad": 0,
        "human_bad_judge_good": 0,
        "human_bad_judge_bad": 0,
    }
    response_disagreements: list[dict[str, Any]] = []
    response_compared = 0
    response_agreements = 0
    button_compared = 0
    button_agreements = 0
    button_disagreements: list[dict[str, Any]] = []
    lead_compared = 0
    lead_agreements = 0
    lead_disagreements: list[dict[str, Any]] = []
    reviewed = 0

    for case in cases:
        human = case.get("human_review")
        if not isinstance(human, Mapping):
            continue
        reviewed += 1
        human_response = human.get("response_acceptable")
        judge_pass = _case_judge_pass(case, judge_metric_name)
        if human_response in {"yes", "no"} and judge_pass is not None:
            human_good = human_response == "yes"
            response_compared += 1
            if human_good == judge_pass:
                response_agreements += 1
            else:
                response_disagreements.append(
                    {
                        "case_id": case["case_id"],
                        "human": human_response,
                        "judge": "pass" if judge_pass else "fail",
                    }
                )
            confusion[
                f"human_{'good' if human_good else 'bad'}_judge_{'good' if judge_pass else 'bad'}"
            ] += 1

        human_button = human.get("button_should_be_shown_now")
        actual = case.get("assistant_output")
        actual_button = actual.get("button_rendered") if isinstance(actual, Mapping) else None
        if human_button in {"yes", "no", "need_more_qualification"} and isinstance(
            actual_button, bool
        ):
            expected_button = human_button == "yes"
            button_compared += 1
            if expected_button == actual_button:
                button_agreements += 1
            else:
                button_disagreements.append(
                    {
                        "case_id": case["case_id"],
                        "human": human_button,
                        "actual_button_rendered": actual_button,
                    }
                )

        expected = case.get("expected")
        authored_lead = expected.get("lead_status") if isinstance(expected, Mapping) else None
        if authored_lead == "unknown":
            authored_lead = "not_enough_data"
        human_lead = human.get("lead_status")
        if human_lead in {"target", "non_target", "not_enough_data"} and authored_lead in {
            "target",
            "non_target",
            "not_enough_data",
        }:
            lead_compared += 1
            if human_lead == authored_lead:
                lead_agreements += 1
            else:
                lead_disagreements.append(
                    {
                        "case_id": case["case_id"],
                        "human": human_lead,
                        "authored_expected": authored_lead,
                    }
                )

    return {
        "schema_version": ALIGNMENT_SCHEMA_VERSION,
        "run_id": merged_run["run_id"],
        "reviewed_cases": reviewed,
        "judge_rule": judge_metric_name or "all_judge_metrics_must_pass",
        "response": {
            "compared": response_compared,
            "agreements": response_agreements,
            "agreement_rate": _rate(response_agreements, response_compared),
            "confusion": confusion,
            "disagreements": response_disagreements,
        },
        "button": {
            "compared": button_compared,
            "agreements": button_agreements,
            "agreement_rate": _rate(button_agreements, button_compared),
            "disagreements": button_disagreements,
        },
        "lead_status": {
            "compared": lead_compared,
            "agreements": lead_agreements,
            "agreement_rate": _rate(lead_agreements, lead_compared),
            "disagreements": lead_disagreements,
        },
    }


def _button_mismatch(case: Mapping[str, Any], human: Mapping[str, Any]) -> bool:
    verdict = human.get("button_should_be_shown_now")
    output = case.get("assistant_output")
    actual = output.get("button_rendered") if isinstance(output, Mapping) else None
    if verdict not in {"yes", "no", "need_more_qualification"} or not isinstance(actual, bool):
        return False
    return actual != (verdict == "yes")


def build_prompt_improvement_packet(merged_run: Mapping[str, Any]) -> dict[str, Any]:
    """Create a compact, human-grounded packet for a prompt-improvement agent.

    A judge-only failure is intentionally not enough to include an item: when a
    human says an answer is good and the judge says it is bad, that is metric
    calibration work, not evidence that the system prompt should be changed.
    """

    cases = _require_run(merged_run)
    error_tags: Counter[str] = Counter()
    items: list[dict[str, Any]] = []
    human_rejected = 0
    button_mismatches = 0
    hard_failures = 0

    for case in cases:
        human = case.get("human_review")
        human = human if isinstance(human, Mapping) else None
        rejected = bool(human and human.get("response_acceptable") == "no")
        mismatch = bool(human and _button_mismatch(case, human))
        failed_hard = [
            copy.deepcopy(check)
            for check in case.get("hard_checks", [])
            if isinstance(check, Mapping) and check.get("passed") is False
        ]
        if not (rejected or mismatch or failed_hard):
            continue

        human_rejected += int(rejected)
        button_mismatches += int(mismatch)
        hard_failures += len(failed_hard)
        if human:
            error_tags.update(tag for tag in human.get("failure_tags", []) if isinstance(tag, str))
        failed_judge = [
            copy.deepcopy(metric)
            for metric in case.get("judge_metrics", [])
            if isinstance(metric, Mapping) and metric.get("passed") is False
        ]
        issues: list[str] = []
        if rejected:
            issues.append("human_rejected_response")
        if mismatch:
            issues.append("human_button_mismatch")
        if failed_hard:
            issues.append("hard_check_failure")
        items.append(
            {
                "case_id": case["case_id"],
                "family": case.get("family"),
                "scope": case.get("scope"),
                "issues": issues,
                "transcript": case.get("transcript"),
                "user_message": case.get("user_message"),
                "assistant_output": copy.deepcopy(case.get("assistant_output")),
                "expected": copy.deepcopy(case.get("expected")),
                "human_review": copy.deepcopy(human),
                "failed_hard_checks": failed_hard,
                "failed_judge_metrics": failed_judge,
            }
        )

    alignment = merged_run.get("alignment")
    if not isinstance(alignment, Mapping):
        alignment = build_alignment_report(merged_run)
    response_alignment = alignment.get("response")
    disagreements = (
        copy.deepcopy(response_alignment.get("disagreements", []))
        if isinstance(response_alignment, Mapping)
        else []
    )
    return {
        "schema_version": PROMPT_PACKET_SCHEMA_VERSION,
        "run_id": merged_run["run_id"],
        "prompt": copy.deepcopy(merged_run.get("prompt")),
        "dataset_version": merged_run.get("dataset_version"),
        "summary": {
            "actionable_cases": len(items),
            "human_rejected_responses": human_rejected,
            "human_button_mismatches": button_mismatches,
            "failed_hard_checks": hard_failures,
            "failure_tag_counts": dict(error_tags.most_common()),
        },
        "metric_calibration_warning": {
            "description": (
                "Human/judge disagreements are listed for metric calibration; "
                "they must not by themselves drive prompt changes."
            ),
            "response_disagreements": disagreements,
        },
        "items": items,
    }


def process_human_feedback(
    run: Mapping[str, Any], review: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """Build all machine-readable artifacts returned by one review cycle."""

    merged = merge_human_feedback(run, review)
    return {
        "merged_run": merged,
        "alignment": copy.deepcopy(merged["alignment"]),
        "prompt_improvement_packet": build_prompt_improvement_packet(merged),
    }
