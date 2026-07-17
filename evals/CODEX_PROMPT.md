# Задание Codex на одну итерацию системного промпта

Используй этот текст после `merge-review`. Подставь реальные пути к run-каталогу и
идентификатору новой версии.

```text
Нужно выполнить одну контролируемую итерацию системного промпта sales-бота.

Прочитай:
- evals/results/<baseline>/prompt_improvement_packet.json
- evals/results/<baseline>/alignment.json
- evals/results/<baseline>/run_with_human_review.json
- prompts/active.json
- prompts/user_chat/<активная-версия>.md

Сначала объясни, какие human-rejected ошибки имеют общий корень. Human labels — источник
бизнес-истины. Конфликт, где эксперт одобрил ответ, а judge отклонил его, не используй как
причину менять промпт: это задача калибровки метрики.

Создай candidate в prompts/user_chat/<new-version>.md, где <new-version> совпадает с
run-id этого теста. Активный и ранее протестированные промпты не меняй. Не меняй код,
dataset, expected behavior, hard checks,
порог judge или сами метрики. Не добавляй в промпт case_id и не оптимизируй ответы под
дословные формулировки сценариев.

Изменения должны быть минимальными и обобщаемыми. Для каждого изменения укажи:
1) какую группу human-rejected ошибок оно исправляет;
2) почему не должно ухудшить уже хорошие сценарии;
3) какой риск регрессии надо проверить.

После правки запусти:
.venv/bin/python -m evals.run_sales_eval run \
  --run-id <new-version> \
  --prompt prompts/user_chat/<new-version>.md

Сравни candidate с baseline по human-критичным case_id, решению о кнопке, hard checks,
Sales Next-Step Quality и Prompt Alignment. Не объявляй candidate лучше только по среднему
judge score. Если human-критичный кейс ухудшился или появилась новая критическая ошибка,
создай следующую версию без спорного правила и повтори максимум ещё один раз.

В конце верни пути к candidate и двум новым HTML-отчётам, короткий diff результатов и
список рисков, которые должен проверить человек. `prompts/active.json` переключай только
после явного принятия новой версии.
```
