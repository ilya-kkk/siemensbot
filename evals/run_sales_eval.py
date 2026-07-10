from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any

from dotenv import load_dotenv

from app.ai.openrouter import OpenRouterClient
from evals.reporting import (
    build_alignment_report,
    build_prompt_improvement_packet,
    merge_human_feedback,
    write_business_review_html,
    write_technical_report_html,
)

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT_DIR / "evals" / "cases" / "sales_v1.jsonl"
DEFAULT_PROMPT = ROOT_DIR / "prompts" / "user_chat.system.md"
DEFAULT_RESULTS_DIR = ROOT_DIR / "evals" / "results"
DEFAULT_FOLLOWUP_TEXT = (
    "Привет. После бесплатного обучения лучше не гадать, а приложить его к твоей "
    "ситуации. В какой нише сейчас проект?"
)
URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)


class ScenarioValidationError(ValueError):
    pass


@dataclass(frozen=True)
class EvalSettings:
    openrouter_api_key: str
    openrouter_model: str
    followup_text: str
    public_base_url: str | None = None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _default_run_id() -> str:
    return datetime.now(UTC).strftime("sales-%Y%m%d-%H%M%S")


def _safe_run_id(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-.")
    if not cleaned:
        raise ValueError("run id must contain at least one letter or digit")
    return cleaned


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_scenarios(path: Path) -> list[dict[str, Any]]:
    required = {
        "case_id",
        "family",
        "transcript",
        "user_message",
        "lead_status",
        "expected_button_action",
        "expected_behavior",
    }
    scenarios: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            item = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ScenarioValidationError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(item, dict):
            raise ScenarioValidationError(f"{path}:{line_number}: scenario must be an object")
        missing = required - set(item)
        if missing:
            raise ScenarioValidationError(
                f"{path}:{line_number}: missing fields: {', '.join(sorted(missing))}"
            )
        case_id = str(item["case_id"])
        if case_id in seen_ids:
            raise ScenarioValidationError(f"{path}:{line_number}: duplicate case_id {case_id!r}")
        if item["lead_status"] not in {"target", "non_target", "unknown"}:
            raise ScenarioValidationError(
                f"{path}:{line_number}: invalid lead_status {item['lead_status']!r}"
            )
        if item["expected_button_action"] not in {
            "show",
            "do_not_show",
            "need_more_qualification",
        }:
            raise ScenarioValidationError(
                f"{path}:{line_number}: invalid expected_button_action "
                f"{item['expected_button_action']!r}"
            )
        hard_constraints = item.get("hard_constraints") or {}
        if not isinstance(hard_constraints, dict):
            raise ScenarioValidationError(
                f"{path}:{line_number}: hard_constraints must be an object"
            )
        declared_offer = hard_constraints.get("should_send_offer")
        expected_offer = item["expected_button_action"] == "show"
        if declared_offer is not None and bool(declared_offer) is not expected_offer:
            raise ScenarioValidationError(
                f"{path}:{line_number}: hard_constraints.should_send_offer conflicts "
                "with expected_button_action"
            )
        item["case_id"] = case_id
        item.setdefault("scope", "turn")
        item.setdefault("hard_constraints", {})
        item.setdefault("tags", [])
        seen_ids.add(case_id)
        scenarios.append(item)
    if not scenarios:
        raise ScenarioValidationError(f"{path}: dataset is empty")
    return scenarios


def _check(
    name: str,
    passed: bool,
    *,
    expected: Any,
    actual: Any,
    reason: str,
) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "expected": expected,
        "actual": actual,
        "reason": "" if passed else reason,
    }


def build_hard_checks(
    scenario: dict[str, Any],
    *,
    reply_text: str,
    should_send_offer: bool,
    button_rendered: bool,
    generation_error: str | None = None,
) -> list[dict[str, Any]]:
    action = scenario["expected_button_action"]
    expected_offer = action == "show"
    constraints = scenario.get("hard_constraints") or {}
    if not isinstance(constraints, dict):
        constraints = {}
    max_questions = int(constraints.get("max_questions", 1))

    checks = [
        _check(
            "generation_succeeded",
            generation_error is None,
            expected="successful response",
            actual=generation_error or "success",
            reason=generation_error or "generation failed",
        ),
        _check(
            "non_empty_reply",
            bool(reply_text.strip()),
            expected="non-empty reply",
            actual=reply_text,
            reason="reply is empty",
        ),
        _check(
            "max_one_question",
            reply_text.count("?") <= max_questions,
            expected=f"at most {max_questions} question mark(s)",
            actual=reply_text.count("?"),
            reason="reply asks too many questions",
        ),
        _check(
            "no_long_dash",
            "—" not in reply_text and "–" not in reply_text,
            expected="no em/en dash",
            actual=reply_text,
            reason="reply contains a forbidden dash character",
        ),
        _check(
            "no_url_in_text",
            URL_RE.search(reply_text) is None,
            expected="no URL in reply text",
            actual=reply_text,
            reason="the application must render the URL as a button",
        ),
        _check(
            "offer_decision",
            should_send_offer is expected_offer,
            expected=expected_offer,
            actual=should_send_offer,
            reason=f"expected button action is {action}",
        ),
        _check(
            "button_rendering",
            button_rendered is expected_offer,
            expected=expected_offer,
            actual=button_rendered,
            reason=f"rendered button does not match expected action {action}",
        ),
    ]

    if scenario["lead_status"] == "non_target":
        checks.append(
            _check(
                "non_target_never_gets_offer",
                not button_rendered,
                expected=False,
                actual=button_rendered,
                reason="a non-target lead must never receive the offer button",
            )
        )

    lowered = reply_text.casefold()
    for phrase in constraints.get("must_not_contain", []):
        phrase_text = str(phrase)
        checks.append(
            _check(
                f"must_not_contain:{phrase_text}",
                phrase_text.casefold() not in lowered,
                expected=f"reply does not contain {phrase_text!r}",
                actual=reply_text,
                reason=f"reply contains forbidden phrase {phrase_text!r}",
            )
        )
    for phrase in constraints.get("must_contain", []):
        phrase_text = str(phrase)
        checks.append(
            _check(
                f"must_contain:{phrase_text}",
                phrase_text.casefold() in lowered,
                expected=f"reply contains {phrase_text!r}",
                actual=reply_text,
                reason=f"reply is missing required phrase {phrase_text!r}",
            )
        )
    required_options = [str(value) for value in constraints.get("must_mention_any", [])]
    if required_options:
        checks.append(
            _check(
                "must_mention_any",
                any(value.casefold() in lowered for value in required_options),
                expected={"one_of": required_options},
                actual=reply_text,
                reason=f"reply must mention one of: {', '.join(required_options)}",
            )
        )
    return checks


async def _generate_case(
    scenario: dict[str, Any],
    *,
    client: OpenRouterClient,
    system_prompt: str,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    error: str | None = None
    reply_text = ""
    should_send_offer = False
    source = "error"
    usage: dict[str, Any] | None = None
    async with semaphore:
        try:
            decision = await client.chat_reply(
                str(scenario["transcript"]),
                str(scenario["user_message"]),
                system_prompt=system_prompt,
            )
            reply_text = decision.reply_text
            should_send_offer = decision.should_send_offer
            source = str(decision.request_payload.get("type") or "llm")
            usage = decision.usage
        except Exception as exc:  # keep the rest of a paid eval run inspectable
            error = f"{type(exc).__name__}: {exc}"

    prior_button = bool(scenario.get("prior_button_rendered", False))
    button_rendered = prior_button or should_send_offer
    hard_checks = build_hard_checks(
        scenario,
        reply_text=reply_text,
        should_send_offer=should_send_offer,
        button_rendered=button_rendered,
        generation_error=error,
    )
    return {
        "case_id": scenario["case_id"],
        "family": scenario["family"],
        "scope": scenario.get("scope", "turn"),
        "transcript": scenario["transcript"],
        "user_message": scenario["user_message"],
        "expected": {
            "lead_status": scenario["lead_status"],
            "button_action": scenario["expected_button_action"],
            "behavior": scenario["expected_behavior"],
            "must_not": list((scenario.get("hard_constraints") or {}).get("must_not_contain", [])),
        },
        "tags": list(scenario.get("tags", [])),
        "assistant_output": {
            "text": reply_text,
            "should_send_offer": should_send_offer,
            "button_rendered": button_rendered,
            "source": source,
            "usage": usage,
            "error": error,
        },
        "hard_checks": hard_checks,
        "judge_metrics": [],
    }


async def generate_cases(
    scenarios: list[dict[str, Any]],
    *,
    settings: EvalSettings,
    system_prompt: str,
    max_concurrent: int,
) -> list[dict[str, Any]]:
    client = OpenRouterClient(settings)  # type: ignore[arg-type]
    semaphore = asyncio.Semaphore(max(1, max_concurrent))
    return list(
        await asyncio.gather(
            *(
                _generate_case(
                    scenario,
                    client=client,
                    system_prompt=system_prompt,
                    semaphore=semaphore,
                )
                for scenario in scenarios
            )
        )
    )


def _judge_input(case: dict[str, Any]) -> str:
    transcript = case["transcript"] or "<диалог ещё не начат>"
    return (
        f"Предыдущий диалог:\n{transcript}\n\n"
        f"Новое сообщение пользователя:\n{case['user_message']}"
    )


def attach_judge_results(
    cases: list[dict[str, Any]],
    *,
    judge_model: str,
    api_key: str,
    deepeval_results_dir: Path,
) -> None:
    from evals.deepeval_adapter import JudgeCase, build_judge, evaluate_cases

    judge_cases = [
        JudgeCase(
            case_id=case["case_id"],
            input_text=_judge_input(case),
            actual_output=case["assistant_output"]["text"],
            expected_behavior=case["expected"]["behavior"],
        )
        for case in cases
        if not case["assistant_output"].get("error")
    ]
    if not judge_cases:
        return
    judge = build_judge(model_name=judge_model, api_key=api_key)
    results = evaluate_cases(
        judge_cases,
        judge=judge,
        results_folder=deepeval_results_dir,
    )
    for case in cases:
        case["judge_metrics"] = [item.to_dict() for item in results.get(case["case_id"], [])]


def build_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    hard_failed_cases = [
        case for case in cases if any(not check["passed"] for check in case["hard_checks"])
    ]
    metrics: dict[str, list[float]] = {}
    metric_passes: dict[str, int] = {}
    for case in cases:
        for metric in case.get("judge_metrics", []):
            score = metric.get("score")
            if isinstance(score, int | float):
                metrics.setdefault(metric["name"], []).append(float(score))
                metric_passes[metric["name"]] = metric_passes.get(metric["name"], 0) + int(
                    bool(metric.get("passed"))
                )
    metric_summary = {
        name: {
            "average_score": round(fmean(scores), 4),
            "passed": metric_passes.get(name, 0),
            "total": len(scores),
        }
        for name, scores in metrics.items()
    }
    return {
        "total_cases": len(cases),
        "hard_passed_cases": len(cases) - len(hard_failed_cases),
        "hard_failed_cases": len(hard_failed_cases),
        "generation_errors": sum(bool(case["assistant_output"].get("error")) for case in cases),
        "buttons_rendered": sum(bool(case["assistant_output"]["button_rendered"]) for case in cases),
        "non_target_buttons": sum(
            case["expected"]["lead_status"] == "non_target"
            and bool(case["assistant_output"]["button_rendered"])
            for case in cases
        ),
        "judge_metrics": metric_summary,
    }


def _business_example(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "scope": case["scope"],
        "transcript": case["transcript"],
        "user_message": case["user_message"],
        "actual_reply": case["assistant_output"]["text"],
        "actual_button_shown": case["assistant_output"]["button_rendered"],
    }


def write_run_artifacts(run: dict[str, Any], run_dir: Path) -> dict[str, Path]:
    run_dir.mkdir(parents=True, exist_ok=True)
    run_json = run_dir / "run.json"
    examples_jsonl = run_dir / "examples.jsonl"
    business_html = run_dir / "business_review.html"
    technical_html = run_dir / "technical_report.html"
    _write_json(run_json, run)
    examples_jsonl.write_text(
        "".join(
            json.dumps(_business_example(case), ensure_ascii=False) + "\n"
            for case in run["cases"]
        ),
        encoding="utf-8",
    )
    write_business_review_html(run, business_html)
    write_technical_report_html(run, technical_html)
    return {
        "run": run_json,
        "examples": examples_jsonl,
        "business_report": business_html,
        "technical_report": technical_html,
    }


def run_command(args: argparse.Namespace) -> int:
    load_dotenv(ROOT_DIR / ".env")
    os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "1")
    dataset_path = Path(args.dataset).resolve()
    prompt_path = Path(args.prompt).resolve()
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required")
    target_model = args.target_model or os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini")
    judge_model = args.judge_model or os.getenv("DEEPEVAL_JUDGE_MODEL", "openai/gpt-4.1")
    settings = EvalSettings(
        openrouter_api_key=api_key,
        openrouter_model=target_model,
        followup_text=os.getenv("FOLLOWUP_TEXT", DEFAULT_FOLLOWUP_TEXT),
        public_base_url=os.getenv("PUBLIC_BASE_URL"),
    )
    scenarios = load_scenarios(dataset_path)
    if args.limit:
        scenarios = scenarios[: args.limit]
    prompt_text = prompt_path.read_text(encoding="utf-8")
    cases = asyncio.run(
        generate_cases(
            scenarios,
            settings=settings,
            system_prompt=prompt_text,
            max_concurrent=args.max_concurrent,
        )
    )
    run_id = _safe_run_id(args.run_id or _default_run_id())
    run_dir = Path(args.output_dir).resolve() / run_id
    if not args.skip_judge:
        attach_judge_results(
            cases,
            judge_model=judge_model,
            api_key=api_key,
            deepeval_results_dir=run_dir / "deepeval",
        )
    run = {
        "schema_version": "sales-eval-run-v1",
        "run_id": run_id,
        "generated_at": _utc_now(),
        "dataset_version": dataset_path.stem,
        "dataset_path": str(dataset_path),
        "prompt": {
            "path": str(prompt_path),
            "sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        },
        "target_model": target_model,
        "judge_model": None if args.skip_judge else judge_model,
        "summary": build_summary(cases),
        "cases": cases,
    }
    paths = write_run_artifacts(run, run_dir)
    print(json.dumps({key: str(path) for key, path in paths.items()}, ensure_ascii=False, indent=2))
    return 0


def merge_command(args: argparse.Namespace) -> int:
    run_path = Path(args.run).resolve()
    labels_path = Path(args.labels).resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else run_path.parent
    run = _read_json(run_path)
    reviewed = _read_json(labels_path)
    merged = merge_human_feedback(run, reviewed)
    merged["summary"] = build_summary(merged["cases"])
    alignment = build_alignment_report(merged)
    packet = build_prompt_improvement_packet(merged)
    _write_json(output_dir / "run_with_human_review.json", merged)
    _write_json(output_dir / "alignment.json", alignment)
    _write_json(output_dir / "prompt_improvement_packet.json", packet)
    write_technical_report_html(merged, output_dir / "technical_report_reviewed.html")
    print(
        json.dumps(
            {
                "merged_run": str(output_dir / "run_with_human_review.json"),
                "alignment": str(output_dir / "alignment.json"),
                "prompt_packet": str(output_dir / "prompt_improvement_packet.json"),
                "technical_report": str(output_dir / "technical_report_reviewed.html"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run and review Siemensbot sales evals")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="generate bot responses and eval reports")
    run_parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    run_parser.add_argument("--prompt", default=str(DEFAULT_PROMPT))
    run_parser.add_argument("--output-dir", default=str(DEFAULT_RESULTS_DIR))
    run_parser.add_argument("--run-id")
    run_parser.add_argument("--target-model")
    run_parser.add_argument("--judge-model")
    run_parser.add_argument("--max-concurrent", type=int, default=4)
    run_parser.add_argument("--limit", type=int)
    run_parser.add_argument(
        "--skip-judge",
        action="store_true",
        help="generate target outputs and reports without paid DeepEval judge calls",
    )
    run_parser.set_defaults(handler=run_command)

    merge_parser = subparsers.add_parser(
        "merge-review", help="merge downloaded human labels back into a run"
    )
    merge_parser.add_argument("--run", required=True)
    merge_parser.add_argument("--labels", required=True)
    merge_parser.add_argument("--output-dir")
    merge_parser.set_defaults(handler=merge_command)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
