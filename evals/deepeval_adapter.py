"""Small, version-specific boundary around DeepEval's judge API.

This module targets DeepEval 4.0.8.  The rest of the eval pipeline should use
the dataclasses below instead of depending on DeepEval's ``TestResult`` and
``MetricData`` models directly.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from deepeval import evaluate
from deepeval.evaluate import AsyncConfig, CacheConfig, DisplayConfig, ErrorConfig
from deepeval.metrics import GEval, PromptAlignmentMetric
from deepeval.models import DeepEvalBaseLLM, OpenRouterModel
from deepeval.test_case import LLMTestCase, SingleTurnParams

SALES_METRIC_NAME = "Sales Next-Step Quality"
PROMPT_ALIGNMENT_METRIC_NAME = "Prompt Alignment"

SALES_CRITERIA = """
Оцени, является ли ACTUAL_OUTPUT правильным следующим ответом в текущем
sales/discovery-диалоге. Сопоставь его с INPUT и EXPECTED_OUTPUT. Ответ должен
учитывать уже известный контекст, соответствовать текущему этапу квалификации и
вести к уместному следующему шагу. Снижай оценку за повторный вопрос, вывод без
достаточных данных, преждевременный совет, диагноз или оффер, пропущенный важный
шаг, давление либо противоречие ожидаемому поведению.
""".strip()

SALES_EVALUATION_STEPS = [
    "Определи по INPUT текущий этап диалога и уже известные факты о лиде.",
    "Извлеки из EXPECTED_OUTPUT требуемое поведение и ограничения следующего шага.",
    "Сравни ACTUAL_OUTPUT с этим поведением, не требуя дословного совпадения.",
    "Проверь, не повторяет ли ответ известное и не делает ли преждевременный вывод или оффер.",
    "Поставь итоговую оценку качества следующего шага и кратко объясни главную причину.",
]

PROMPT_INSTRUCTIONS = [
    "Ответ содержит не более одного вопроса.",
    "Ответ краткий и естественный для переписки в Telegram.",
    "Ответ не дает консультационных советов вместо продолжения диагностики.",
    "Ответ не давит на пользователя и не обещает результат, доход или гарантированный эффект.",
    "Ответ не выдумывает факты о пользователе, его проекте или результатах.",
    "Ответ не содержит URL или ссылку текстом.",
]


@dataclass(frozen=True, slots=True)
class JudgeCase:
    """One generated bot response ready for LLM-as-a-judge evaluation."""

    case_id: str
    input_text: str
    actual_output: str
    expected_behavior: str


@dataclass(frozen=True, slots=True)
class JudgeResult:
    """Stable, JSON-friendly projection of DeepEval's per-metric result."""

    metric_name: str
    score: float | None
    threshold: float
    passed: bool
    reason: str | None
    error: str | None
    evaluation_model: str | None

    def to_dict(self) -> dict[str, str | float | bool | None]:
        """Return the report schema used by the eval runner."""

        return {
            "name": self.metric_name,
            "score": self.score,
            "threshold": self.threshold,
            "passed": self.passed,
            "reason": self.reason,
            "error": self.error,
            "evaluation_model": self.evaluation_model,
        }


def create_openrouter_judge(
    model_name: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> OpenRouterModel:
    """Create a deterministic DeepEval judge backed by OpenRouter.

    When ``api_key`` is omitted, DeepEval reads ``OPENROUTER_API_KEY``.  GEval
    requires the selected upstream model to expose token log probabilities;
    ``openai/gpt-4.1`` is the recommended known-compatible choice.
    """

    if not model_name.strip():
        raise ValueError("model_name must not be empty")

    return OpenRouterModel(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=0,
    )


def build_judge(
    model_name: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> OpenRouterModel:
    """Backward-compatible, concise name used by the eval runner."""

    return create_openrouter_judge(
        model_name=model_name,
        api_key=api_key,
        base_url=base_url,
    )


def evaluate_cases(
    cases: Sequence[JudgeCase],
    judge: DeepEvalBaseLLM,
    results_folder: str | Path | None = None,
) -> dict[str, list[JudgeResult]]:
    """Evaluate all cases in one DeepEval run and return results by case ID.

    DeepEval errors are retained as ``JudgeResult.error`` so one malformed or
    rate-limited judge response does not discard the rest of a paid batch.  If
    ``results_folder`` is provided, DeepEval also writes its full timestamped
    ``test_run_*.json`` there.
    """

    case_list = list(cases)
    if not case_list:
        return {}

    _validate_case_ids(case_list)

    test_cases = [
        LLMTestCase(
            name=case.case_id,
            input=case.input_text,
            actual_output=case.actual_output,
            expected_output=case.expected_behavior,
            metadata={"case_id": case.case_id},
        )
        for case in case_list
    ]

    metrics = [
        GEval(
            name=SALES_METRIC_NAME,
            evaluation_params=[
                SingleTurnParams.INPUT,
                SingleTurnParams.ACTUAL_OUTPUT,
                SingleTurnParams.EXPECTED_OUTPUT,
            ],
            criteria=SALES_CRITERIA,
            evaluation_steps=SALES_EVALUATION_STEPS,
            model=judge,
            threshold=0.7,
            async_mode=True,
        ),
        PromptAlignmentMetric(
            prompt_instructions=PROMPT_INSTRUCTIONS,
            model=judge,
            threshold=0.8,
            include_reason=True,
            async_mode=True,
        ),
    ]

    evaluation = evaluate(
        test_cases=test_cases,
        metrics=metrics,
        identifier="siemensbot-sales-eval",
        hyperparameters={
            "judge_model": judge.get_model_name(),
            "sales_metric_version": "v1",
        },
        async_config=AsyncConfig(run_async=True, max_concurrent=5),
        display_config=DisplayConfig(
            show_indicator=False,
            print_results=False,
            results_folder=(str(results_folder) if results_folder is not None else None),
            inspect_after_run=False,
        ),
        cache_config=CacheConfig(use_cache=False, write_cache=False),
        error_config=ErrorConfig(ignore_errors=True, skip_on_missing_params=False),
    )

    results: dict[str, list[JudgeResult]] = {case.case_id: [] for case in case_list}
    for test_result in evaluation.test_results:
        metadata = test_result.metadata or {}
        case_id = str(metadata.get("case_id") or test_result.name)
        if case_id not in results:
            raise RuntimeError(f"DeepEval returned an unknown case ID: {case_id}")

        for metric_data in test_result.metrics_data or []:
            results[case_id].append(
                JudgeResult(
                    metric_name=metric_data.name,
                    score=metric_data.score,
                    threshold=metric_data.threshold,
                    passed=metric_data.success,
                    reason=metric_data.reason,
                    error=metric_data.error,
                    evaluation_model=metric_data.evaluation_model,
                )
            )

    return results


def _validate_case_ids(cases: Sequence[JudgeCase]) -> None:
    seen: set[str] = set()
    for case in cases:
        if not case.case_id.strip():
            raise ValueError("case_id must not be empty")
        if case.case_id in seen:
            raise ValueError(f"duplicate case_id: {case.case_id}")
        seen.add(case.case_id)
