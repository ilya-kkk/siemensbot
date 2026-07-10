# Sales evals: DeepEval + экспертная разметка

Этот контур запускается отдельно от Telegram и базы данных. Runner импортирует тот же
`OpenRouterClient.chat_reply()`, который отвечает пользователю в production, подставляет
системный промпт из файла и прогоняет синтетические checkpoint-сценарии.

HTTP-ручка для этого не нужна. Внешний контракт Telegram не меняется, а сценарии не
создают пользователей, диалоги или клики в production.

## Что именно измеряется

Для каждого checkpoint проверяется один следующий ответ бота и решение о кнопке.

1. Детерминированные hard checks: ответ не пустой, не больше одного вопроса, нет URL и
   длинного тире, корректно ли выставлен `should_send_offer`, показана ли кнопка и не
   показана ли она заведомо нецелевому лиду.
2. `Sales Next-Step Quality` через DeepEval `GEval`: LLM-as-a-judge сравнивает ответ с
   описанием правильного следующего шага, но не требует дословного эталона.
3. `Prompt Alignment`: judge отдельно проверяет короткий Telegram-стиль, отсутствие
   советов, давления, обещаний, выдуманных фактов и URL.

Judge оценивает качество ответа, но не считается бизнес-истиной. Истина для калибровки —
разметка заказчика. Реальный клик по кнопке остаётся online-метрикой production; offline
контур может проверить только то, должна ли кнопка появиться и появилась ли она.

## Установка

Из корня репозитория:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,eval]"
```

Обязательно нужен уже используемый приложением ключ:

```dotenv
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=...
```

Необязательно:

```dotenv
# Judge для GEval должен поддерживать logprobs.
DEEPEVAL_JUDGE_MODEL=openai/gpt-4.1

# По умолчанию runner сам отключает анонимную телеметрию DeepEval.
DEEPEVAL_TELEMETRY_OPT_OUT=1
```

DeepEval закреплён на версии `4.0.8`, потому что его Python API меняется между версиями.
Оба LLM-вызова — target и judge — идут через OpenRouter.

## Первый baseline

Сначала дешёвый smoke на двух сценариях:

```bash
.venv/bin/python -m evals.run_sales_eval run \
  --run-id smoke-v0 \
  --limit 2
```

Затем полный baseline на 24 сценариях:

```bash
.venv/bin/python -m evals.run_sales_eval run --run-id baseline-v0
```

Если пока нужны только ответы модели и hard checks без платного judge:

```bash
.venv/bin/python -m evals.run_sales_eval run \
  --run-id baseline-no-judge \
  --skip-judge
```

Каждый прогон создаёт отдельный каталог в `evals/results/<run-id>/`:

```text
run.json                    полный машиночитаемый результат
examples.jsonl              слепые примеры без judge и ожиданий
business_review.html        автономная форма для заказчика
technical_report.html       полный внутренний отчёт
deepeval/test_run_*.json    сырой экспорт DeepEval
```

`business_review.html` можно отправить заказчику как один файл. Он работает локально в
браузере, автоматически сохраняет черновик в `localStorage` и ничего никуда не отправляет.
Заказчик отмечает:

- целевой ли лид;
- можно ли отправить ответ без исправлений;
- должна ли сейчас появиться кнопка;
- типы ошибок;
- при желании — правильное поведение и пример ответа.

После заполнения кнопка «Скачать reviewed.json» создаёт файл
`reviewed-<run-id>.json`. Не отправляйте заказчику технический отчёт: ожидания сценария и
мнение judge могут сместить его оценку.

## Вернуть экспертную оценку в прогон

```bash
.venv/bin/python -m evals.run_sales_eval merge-review \
  --run evals/results/baseline-v0/run.json \
  --labels /path/to/reviewed-baseline-v0.json
```

Команда строго сопоставляет ответы по `case_id` и создаёт:

```text
run_with_human_review.json       полный прогон с human labels
alignment.json                   совпадения и конфликты human ↔ judge/dataset
prompt_improvement_packet.json   компактный вход для Codex
technical_report_reviewed.html   внутренний HTML после разметки
```

Judge-only ошибка намеренно не попадает в задачи на изменение промпта, если эксперт
считает ответ хорошим. Такой конфликт сначала исправляется в метрике или ожидании
сценария. Метрику и системный промпт нельзя менять одновременно: сначала калибруем и
фиксируем метрику, потом сравниваем версии промпта.

## Один цикл улучшения промпта

1. Зафиксировать `baseline-v0` и получить reviewed JSON.
2. Проверить `alignment.json`. Разобрать конфликты human/judge до изменения промпта.
3. Скопировать текущий промпт в отдельный candidate-файл, например
   `evals/candidates/user_chat.v1.md`.
4. Дать Codex `prompt_improvement_packet.json` и candidate-файл с инструкцией:
   «Меняй только candidate prompt; не меняй dataset, hard checks и judge; объясни каждое
   правило через human-rejected кейсы; не оптимизируйся под дословные ответы».
   Готовый текст задания лежит в `evals/CODEX_PROMPT.md`.
5. Прогнать тот же dataset на candidate:

```bash
.venv/bin/python -m evals.run_sales_eval run \
  --run-id candidate-v1 \
  --prompt evals/candidates/user_chat.v1.md
```

6. Сравнить `technical_report.html` baseline и candidate: сначала human-критичные кейсы и
   решение о кнопке, затем hard checks, затем judge score/reasons. Новый business HTML
   можно снова отдать заказчику вслепую.
7. Только после принятия candidate перенести изменения в
   `prompts/user_chat.system.md` и выполнить финальный полный прогон.

Хэш промпта сохраняется в каждом `run.json`, поэтому результаты нельзя случайно спутать.

## Сценарии

Набор находится в `evals/cases/sales_v1.jsonl`: один JSON-объект на строку. `transcript`
содержит только предыдущие реплики; текущее сообщение лежит в `user_message`.

Минимальный контракт:

```json
{
  "case_id": "stable-unique-id",
  "family": "qualification",
  "transcript": "outgoing: В какой нише сейчас проект?",
  "user_message": "Фитнес",
  "lead_status": "unknown",
  "expected_button_action": "need_more_qualification",
  "expected_behavior": "Одним вопросом уточнить продукт и не показывать кнопку.",
  "hard_constraints": {
    "should_send_offer": false,
    "max_questions": 1
  },
  "tags": ["qualification"]
}
```

Допустимые `lead_status`: `target`, `non_target`, `unknown`. Допустимые действия кнопки:
`show`, `do_not_show`, `need_more_qualification`. `case_id` нельзя переиспользовать или
менять после отправки business-отчёта заказчику.

Сейчас набор состоит из checkpoint-тестов одного следующего ответа. Полные симуляции
многоходового диалога стоит добавлять отдельным dataset после калибровки этой первой
метрики, чтобы не смешивать ошибки отдельных ответов с ошибками симулятора пользователя.
