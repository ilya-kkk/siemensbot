from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any

from dotenv import load_dotenv

from app.ai.openrouter import OpenRouterClient
from evals.regression import (
    ROOT_DIR,
    RegressionSuiteValidationError,
    load_jsonl,
    load_regression_suite,
    requirement_index,
    resolve_repo_path,
    validate_regression_suite,
)
from evals.run_sales_eval import EvalSettings


DEFAULT_SUITE = ROOT_DIR / "evals" / "requirements" / "sales_regression_v1.json"
DEFAULT_RESULTS_DIR = ROOT_DIR / "evals" / "results"
DEFAULT_CLIENT_PROMPT = ROOT_DIR / "prompts" / "eval_client" / "v1.md"
DEFAULT_JUDGE_PROMPT = ROOT_DIR / "prompts" / "eval_dialogue_judge" / "v1.md"
SIMULATION_CARD_PLACEHOLDER = "{{SIMULATION_CARD}}"
URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)

SIMULATED_USER_SCHEMA: dict[str, Any] = {
    "name": "simulated_customer_action",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["action", "text", "used_fact_ids", "patience_delta", "reason"],
        "properties": {
            "action": {
                "type": "string",
                "enum": ["reply", "silence", "leave", "accept", "decline"],
            },
            "text": {"type": "string", "maxLength": 1000},
            "used_fact_ids": {
                "type": "array",
                "items": {"type": "string"},
                "uniqueItems": True,
            },
            "patience_delta": {"type": "integer", "minimum": -3, "maximum": 1},
            "reason": {"type": "string", "maxLength": 300},
        },
    },
}

DIALOGUE_JUDGE_SCHEMA: dict[str, Any] = {
    "name": "sales_dialogue_judgement",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "score",
            "passed",
            "summary",
            "requirement_results",
            "invariant_results",
        ],
        "properties": {
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "passed": {"type": "boolean"},
            "summary": {"type": "string", "maxLength": 1000},
            "requirement_results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["requirement_id", "passed", "reason", "evidence"],
                    "properties": {
                        "requirement_id": {"type": "string"},
                        "passed": {"type": "boolean"},
                        "reason": {"type": "string", "maxLength": 700},
                        "evidence": {"type": "string", "maxLength": 700},
                    },
                },
            },
            "invariant_results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["invariant_id", "passed", "reason", "evidence"],
                    "properties": {
                        "invariant_id": {"type": "string"},
                        "passed": {"type": "boolean"},
                        "reason": {"type": "string", "maxLength": 700},
                        "evidence": {"type": "string", "maxLength": 700},
                    },
                },
            },
        },
    },
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_metadata(path: Path) -> dict[str, str]:
    content = path.read_bytes()
    return {
        "path": str(path),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT_DIR,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_prompt(path: Path, *, placeholder: str | None = None) -> str:
    text = path.read_text(encoding="utf-8")
    if placeholder is not None and text.count(placeholder) != 1:
        raise ValueError(f"{path} must contain exactly one {placeholder!r}")
    return text


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def load_personas(path: Path) -> dict[str, dict[str, Any]]:
    return {
        str(row["persona_id"]): _public_row(row)
        for row in load_jsonl(path, "persona_id")
    }


def load_conversation_cases(path: Path) -> list[dict[str, Any]]:
    return [_public_row(row) for row in load_jsonl(path, "case_id")]


def _simulation_system_prompt(
    template: str,
    *,
    persona: dict[str, Any],
    revealed_fact_ids: set[str],
    patience: int,
) -> str:
    card = {
        "summary": persona["summary"],
        "style": persona["style"],
        "facts": persona["facts"],
        "response_policy": persona["response_policy"],
        "offer_behavior": persona["offer_behavior"],
        "exit_policy": persona["exit_policy"],
        "state": {
            "revealed_fact_ids": sorted(revealed_fact_ids),
            "patience": patience,
        },
    }
    return template.replace(
        SIMULATION_CARD_PLACEHOLDER,
        json.dumps(card, ensure_ascii=False, indent=2),
    )


async def _structured_completion(
    client: OpenRouterClient,
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    schema: dict[str, Any],
    temperature: float,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    payload = {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_schema", "json_schema": schema},
    }
    response = await client._chat_completion(payload)  # eval-only structured model call
    try:
        content = response["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid structured model response: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("structured model response must be an object")
    return parsed, response.get("usage")


async def generate_customer_action(
    client: OpenRouterClient,
    *,
    model: str,
    prompt_template: str,
    persona: dict[str, Any],
    transcript: str,
    revealed_fact_ids: set[str],
    patience: int,
    temperature: float,
) -> dict[str, Any]:
    parsed, usage = await _structured_completion(
        client,
        model=model,
        system_prompt=_simulation_system_prompt(
            prompt_template,
            persona=persona,
            revealed_fact_ids=revealed_fact_ids,
            patience=patience,
        ),
        user_prompt=(
            "Выбери естественное следующее действие пользователя после последней реплики "
            "бота. Если отвечаешь, дай ровно одну реплику.\n\n"
            f"Диалог:\n{transcript}"
        ),
        schema=SIMULATED_USER_SCHEMA,
        temperature=temperature,
    )
    action = parsed.get("action")
    text = str(parsed.get("text") or "").strip()
    if action in {"reply", "accept", "decline"} and not text:
        raise RuntimeError(f"simulator action {action!r} requires non-empty text")
    if action in {"silence", "leave"} and text:
        raise RuntimeError(f"simulator action {action!r} requires empty text")
    fact_ids = {
        str(fact["fact_id"])
        for fact in persona["facts"]
        if isinstance(fact, dict) and isinstance(fact.get("fact_id"), str)
    }
    used_fact_ids = parsed.get("used_fact_ids")
    if not isinstance(used_fact_ids, list) or not all(
        isinstance(item, str) for item in used_fact_ids
    ):
        raise RuntimeError("simulator used_fact_ids must be a list of strings")
    unknown_fact_ids = set(used_fact_ids) - fact_ids
    if unknown_fact_ids:
        raise RuntimeError(
            "simulator referenced unknown fact IDs: " + ", ".join(sorted(unknown_fact_ids))
        )
    patience_delta = parsed.get("patience_delta")
    if not isinstance(patience_delta, int) or not -3 <= patience_delta <= 1:
        raise RuntimeError("simulator patience_delta must be an integer from -3 to 1")
    return {
        "action": action,
        "text": text,
        "used_fact_ids": used_fact_ids,
        "patience_delta": patience_delta,
        "reason": str(parsed.get("reason") or ""),
        "usage": usage,
    }


def _turn_hard_checks(
    *,
    reply_text: str,
    should_send_offer: bool,
    lead_status: str,
    error: str | None,
) -> list[dict[str, Any]]:
    question_count = reply_text.count("?")
    has_forbidden_dash = "—" in reply_text or "–" in reply_text
    checks = [
        {
            "name": "generation_succeeded",
            "passed": error is None,
            "reason": error or "",
        },
        {
            "name": "non_empty_reply",
            "passed": bool(reply_text.strip()),
            "reason": "reply is empty" if not reply_text.strip() else "",
        },
        {
            "name": "max_one_question",
            "passed": question_count <= 1,
            "reason": "reply asks more than one question" if question_count > 1 else "",
        },
        {
            "name": "no_long_dash",
            "passed": not has_forbidden_dash,
            "reason": "reply contains a forbidden dash" if has_forbidden_dash else "",
        },
        {
            "name": "no_url_in_text",
            "passed": URL_RE.search(reply_text) is None,
            "reason": "reply contains a URL" if URL_RE.search(reply_text) else "",
        },
    ]
    if lead_status == "non_target":
        checks.append(
            {
                "name": "non_target_never_gets_offer",
                "passed": not should_send_offer,
                "reason": "non-target persona received the offer" if should_send_offer else "",
            }
        )
    return checks


def _dialogue_hard_checks(
    case: dict[str, Any], turns: list[dict[str, Any]], *, stop_reason: str
) -> list[dict[str, Any]]:
    button_outcome = case["expected"]["button_outcome"]
    offered_turns = [
        int(turn["turn"])
        for turn in turns
        if turn["assistant"].get("should_send_offer")
    ]
    offered = bool(offered_turns)
    expected_button = button_outcome == "eventual_button"
    turn_checks_passed = all(
        check["passed"] for turn in turns for check in turn.get("hard_checks", [])
    )
    simulation_valid = stop_reason not in {
        "target_generation_error",
        "simulator_generation_error",
    }
    earliest_button_turn = case["expected"].get("earliest_button_turn")
    button_timing_passed = (
        not offered
        or earliest_button_turn is None
        or offered_turns[0] >= int(earliest_button_turn)
    )
    checks = [
        {
            "name": "simulation_valid",
            "passed": simulation_valid,
            "expected": "no target or simulator infrastructure error",
            "actual": stop_reason,
            "reason": "trajectory is invalid and cannot count as a prompt pass"
            if not simulation_valid
            else "",
        },
        {
            "name": "all_turn_hard_checks",
            "passed": turn_checks_passed,
            "expected": True,
            "actual": turn_checks_passed,
            "reason": "one or more bot turns failed deterministic checks"
            if not turn_checks_passed
            else "",
        },
        {
            "name": "dialogue_button_outcome",
            "passed": offered is expected_button
            if button_outcome != "not_required"
            else not offered,
            "expected": button_outcome,
            "actual": "button_shown" if offered else "no_button",
            "reason": (
                f"expected {button_outcome}, got "
                f"{'button_shown' if offered else 'no_button'}"
            )
            if (
                (button_outcome == "eventual_button" and not offered)
                or (button_outcome != "eventual_button" and offered)
            )
            else "",
        },
        {
            "name": "minimum_dialogue_progress",
            "passed": len(turns) >= int(case["expected"]["minimum_assistant_turns"]),
            "expected": case["expected"]["minimum_assistant_turns"],
            "actual": len(turns),
            "reason": "dialogue ended before minimum observable progress",
        },
        {
            "name": "no_premature_button",
            "passed": button_timing_passed,
            "expected": earliest_button_turn,
            "actual": offered_turns[0] if offered_turns else None,
            "reason": "button appeared before the earliest allowed turn"
            if not button_timing_passed
            else "",
        },
    ]
    return checks


async def simulate_case(
    case: dict[str, Any],
    *,
    persona: dict[str, Any],
    rollout: int,
    client: OpenRouterClient,
    target_prompt: str,
    simulator_model: str,
    simulator_prompt: str,
    simulator_temperature: float,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    async with semaphore:
        transcript = f"outgoing: {case['initial_bot_message']}"
        current_user_message = str(case["initial_user_message"])
        revealed_fact_ids = set(case["initial_revealed_fact_ids"])
        patience = int(persona["exit_policy"].get("initial_patience", 5))
        stop_after_bot_reply = False
        final_customer_action = "reply"
        events: list[dict[str, Any]] = []
        turns: list[dict[str, Any]] = []
        stop_reason = "max_assistant_turns"

        for turn_number in range(1, int(case["max_assistant_turns"]) + 1):
            error: str | None = None
            reply_text = ""
            should_send_offer = False
            source = "error"
            usage: dict[str, Any] | None = None
            try:
                decision = await client.chat_reply(
                    transcript,
                    current_user_message,
                    system_prompt=target_prompt,
                )
                reply_text = decision.reply_text
                should_send_offer = decision.should_send_offer
                source = str(decision.request_payload.get("type") or "llm")
                usage = decision.usage
            except Exception as exc:  # retain partial paid runs for inspection
                error = f"{type(exc).__name__}: {exc}"

            transcript = (
                f"{transcript}\nincoming: {current_user_message}\noutgoing: {reply_text}"
            )
            turn = {
                "turn": turn_number,
                "user": {"text": current_user_message, "action": final_customer_action},
                "assistant": {
                    "text": reply_text,
                    "should_send_offer": should_send_offer,
                    "source": source,
                    "usage": usage,
                    "error": error,
                },
            }
            turn["hard_checks"] = _turn_hard_checks(
                reply_text=reply_text,
                should_send_offer=should_send_offer,
                lead_status=str(case["expected"]["lead_status"]),
                error=error,
            )
            turns.append(turn)

            if error:
                stop_reason = "target_generation_error"
                break
            if should_send_offer:
                stop_reason = "offer_shown"
                break
            if stop_after_bot_reply:
                stop_reason = (
                    "customer_declined" if final_customer_action == "decline" else "customer_ended"
                )
                break

            try:
                customer_action = await generate_customer_action(
                    client,
                    model=simulator_model,
                    prompt_template=simulator_prompt,
                    persona=persona,
                    transcript=transcript,
                    revealed_fact_ids=revealed_fact_ids,
                    patience=patience,
                    temperature=simulator_temperature,
                )
            except Exception as exc:
                turn["simulator_error"] = f"{type(exc).__name__}: {exc}"
                stop_reason = "simulator_generation_error"
                break

            revealed_fact_ids.update(customer_action["used_fact_ids"])
            patience += int(customer_action["patience_delta"])
            event = {
                "after_turn": turn_number,
                **customer_action,
                "patience_after": patience,
                "revealed_fact_ids_after": sorted(revealed_fact_ids),
            }
            events.append(event)
            turn["simulator"] = event
            final_customer_action = str(customer_action["action"])
            if final_customer_action in {"silence", "leave"}:
                stop_reason = f"customer_{final_customer_action}"
                break
            current_user_message = str(customer_action["text"])
            stop_after_bot_reply = final_customer_action in {"accept", "decline"} or patience <= 0

        return {
            "case_id": case["case_id"],
            "persona_id": case["persona_id"],
            "rollout": rollout,
            "provenance": persona["provenance"],
            "requirement_ids": list(case["requirement_ids"]),
            "expected": case["expected"],
            "trajectory_invariants": case["trajectory_invariants"],
            "transcript": transcript,
            "turns": turns,
            "customer_events": events,
            "revealed_fact_ids": sorted(revealed_fact_ids),
            "stop_reason": stop_reason,
            "hard_checks": _dialogue_hard_checks(case, turns, stop_reason=stop_reason),
            "judge": None,
        }


def _normalize_judge_items(
    returned_items: Any,
    *,
    id_field: str,
    expected_ids: list[str],
) -> list[dict[str, Any]]:
    returned = {
        str(item.get(id_field)): item
        for item in returned_items or []
        if isinstance(item, dict)
    }
    normalized: list[dict[str, Any]] = []
    for expected_id in expected_ids:
        item = returned.get(expected_id)
        if item is None:
            normalized.append(
                {
                    id_field: expected_id,
                    "passed": False,
                    "reason": "judge omitted this required criterion",
                    "evidence": "",
                }
            )
        else:
            normalized.append(item)
    return normalized


async def judge_dialogue(
    result: dict[str, Any],
    *,
    case: dict[str, Any],
    persona: dict[str, Any],
    requirements: dict[str, dict[str, Any]],
    client: OpenRouterClient,
    judge_model: str,
    judge_prompt: str,
) -> dict[str, Any]:
    requirement_ids = list(case["requirement_ids"])
    judge_input = {
        "client_truth": {
            "summary": persona["summary"],
            "facts": persona["facts"],
            "style": persona["style"],
            "response_policy": persona["response_policy"],
        },
        "revealed_fact_ids": result["revealed_fact_ids"],
        "expected_trajectory": case["expected"],
        "requirements": [
            {
                "requirement_id": requirement_id,
                "title": requirements[requirement_id]["title"],
                "criteria": requirements[requirement_id]["criteria"],
            }
            for requirement_id in requirement_ids
        ],
        "trajectory_invariants": case["trajectory_invariants"],
        "dialogue": result["transcript"],
        "customer_events": result["customer_events"],
    }
    parsed, usage = await _structured_completion(
        client,
        model=judge_model,
        system_prompt=judge_prompt,
        user_prompt=json.dumps(judge_input, ensure_ascii=False, indent=2),
        schema=DIALOGUE_JUDGE_SCHEMA,
        temperature=0,
    )
    requirement_results = _normalize_judge_items(
        parsed.get("requirement_results"),
        id_field="requirement_id",
        expected_ids=requirement_ids,
    )
    invariant_ids = [str(item["invariant_id"]) for item in case["trajectory_invariants"]]
    invariant_results = _normalize_judge_items(
        parsed.get("invariant_results"),
        id_field="invariant_id",
        expected_ids=invariant_ids,
    )
    critical_invariant_ids = {
        str(item["invariant_id"])
        for item in case["trajectory_invariants"]
        if item["severity"] == "critical"
    }
    critical_invariants_passed = all(
        item["passed"]
        for item in invariant_results
        if item["invariant_id"] in critical_invariant_ids
    )
    parsed["requirement_results"] = requirement_results
    parsed["invariant_results"] = invariant_results
    parsed["passed"] = (
        bool(parsed.get("passed"))
        and all(bool(item.get("passed")) for item in requirement_results)
        and critical_invariants_passed
    )
    parsed["usage"] = usage
    return parsed


async def run_dialogues(
    cases: list[dict[str, Any]],
    *,
    personas: dict[str, dict[str, Any]],
    client: OpenRouterClient,
    target_prompt: str,
    simulator_model: str,
    simulator_prompt: str,
    simulator_temperature: float,
    judge_model: str | None,
    judge_prompt: str,
    requirements: dict[str, dict[str, Any]],
    max_concurrent: int,
    rollouts: int,
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(max(1, max_concurrent))
    work = [
        (case, personas[str(case["persona_id"])], rollout)
        for case in cases
        for rollout in range(1, rollouts + 1)
    ]
    results = list(
        await asyncio.gather(
            *(
                simulate_case(
                    case,
                    persona=persona,
                    rollout=rollout,
                    client=client,
                    target_prompt=target_prompt,
                    simulator_model=simulator_model,
                    simulator_prompt=simulator_prompt,
                    simulator_temperature=simulator_temperature,
                    semaphore=semaphore,
                )
                for case, persona, rollout in work
            )
        )
    )
    if judge_model:
        judge_semaphore = asyncio.Semaphore(max(1, min(max_concurrent, 3)))

        async def attach(
            result: dict[str, Any], case: dict[str, Any], persona: dict[str, Any]
        ) -> None:
            async with judge_semaphore:
                try:
                    result["judge"] = await judge_dialogue(
                        result,
                        case=case,
                        persona=persona,
                        requirements=requirements,
                        client=client,
                        judge_model=judge_model,
                        judge_prompt=judge_prompt,
                    )
                except Exception as exc:
                    result["judge"] = {
                        "passed": False,
                        "score": 0,
                        "summary": "",
                        "requirement_results": [],
                        "invariant_results": [],
                        "error": f"{type(exc).__name__}: {exc}",
                    }

        await asyncio.gather(
            *(
                attach(result, case, persona)
                for result, (case, persona, _rollout) in zip(results, work, strict=True)
            )
        )
    return results


def build_dialogue_summary(dialogues: list[dict[str, Any]]) -> dict[str, Any]:
    hard_passes = [
        all(check["passed"] for check in dialogue["hard_checks"])
        for dialogue in dialogues
    ]
    judged = [
        dialogue
        for dialogue in dialogues
        if isinstance(dialogue.get("judge"), dict)
    ]
    judge_scores = [
        float(dialogue["judge"]["score"])
        for dialogue in judged
        if isinstance(dialogue["judge"].get("score"), int | float)
    ]
    judge_passes = [bool(dialogue["judge"].get("passed")) for dialogue in judged]
    release_gate_evaluable = len(judged) == len(dialogues)
    release_gate_passed = (
        release_gate_evaluable and all(hard_passes) and all(judge_passes)
    )
    return {
        "total_rollouts": len(dialogues),
        "unique_cases": len({dialogue["case_id"] for dialogue in dialogues}),
        "hard_passed_rollouts": sum(hard_passes),
        "hard_failed_rollouts": len(dialogues) - sum(hard_passes),
        "judge_passed_rollouts": sum(judge_passes),
        "judge_failed_rollouts": len(judged) - sum(judge_passes),
        "average_judge_score": round(fmean(judge_scores), 2) if judge_scores else None,
        "offers_shown": sum(
            any(turn["assistant"].get("should_send_offer") for turn in dialogue["turns"])
            for dialogue in dialogues
        ),
        "total_bot_turns": sum(len(dialogue["turns"]) for dialogue in dialogues),
        "release_gate_evaluable": release_gate_evaluable,
        "release_gate_passed": release_gate_passed,
    }


def _write_markdown_report(run: dict[str, Any], path: Path) -> None:
    summary = run["summary"]
    lines = [
        f"# Dialogue eval: {run['run_id']}",
        "",
        f"Prompt: `{run['prompts']['target']['path']}`",
        f"Simulator: `{run['simulator_model']}`",
        f"Judge: `{run['judge_model'] or 'disabled'}`",
        f"Rollouts per case: `{run['rollouts_per_case']}`",
        "",
        "## Summary",
        "",
    ]
    lines.extend(f"- {key}: {value}" for key, value in summary.items())
    for dialogue in run["dialogues"]:
        hard_passed = all(item["passed"] for item in dialogue["hard_checks"])
        lines.extend(
            [
                "",
                f"## {dialogue['case_id']} / rollout {dialogue['rollout']}",
                "",
                f"Persona: `{dialogue['persona_id']}`",
                f"Stop reason: `{dialogue['stop_reason']}`",
                "",
                "```text",
                dialogue["transcript"],
                "```",
                "",
                f"Hard checks: {'PASS' if hard_passed else 'FAIL'}",
            ]
        )
        judge = dialogue.get("judge")
        if isinstance(judge, dict):
            lines.append(
                f"Judge: {'PASS' if judge.get('passed') else 'FAIL'} "
                f"({judge.get('score', 0)}/100) - {judge.get('summary', '')}"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_command(args: argparse.Namespace) -> int:
    report = validate_regression_suite(Path(args.suite).resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def run_command(args: argparse.Namespace) -> int:
    load_dotenv(ROOT_DIR / ".env")
    suite_path = Path(args.suite).resolve()
    validation = validate_regression_suite(suite_path)
    suite = load_regression_suite(suite_path)
    prompt_path = Path(args.prompt).resolve()
    run_id = str(args.run_id)
    if prompt_path.stem != run_id:
        raise ValueError(
            f"run-id {run_id!r} must match prompt version {prompt_path.stem!r}"
        )
    if args.rollouts < 1:
        raise ValueError("rollouts must be at least 1")
    if not 0 <= args.simulator_temperature <= 2:
        raise ValueError("simulator-temperature must be from 0 to 2")
    run_dir = Path(args.output_dir).resolve() / run_id
    run_path = run_dir / "dialogue_run.json"
    examples_path = run_dir / "dialogue_examples.jsonl"
    report_path = run_dir / "dialogue_report.md"
    existing_artifacts = [
        path for path in (run_path, examples_path, report_path) if path.exists()
    ]
    if existing_artifacts:
        raise FileExistsError(
            "refusing to overwrite immutable eval artifacts: "
            + ", ".join(str(path) for path in existing_artifacts)
        )
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is required")
    target_model = args.target_model or os.getenv(
        "OPENROUTER_MODEL", "openai/gpt-4.1-mini"
    )
    simulator_model = args.simulator_model or os.getenv(
        "OPENROUTER_SIMULATOR_MODEL", "openai/gpt-4.1-mini"
    )
    judge_model = None
    if not args.skip_judge:
        judge_model = args.judge_model or os.getenv(
            "DEEPEVAL_JUDGE_MODEL", "openai/gpt-4.1"
        )
    client_prompt_path = Path(args.client_prompt).resolve()
    judge_prompt_path = Path(args.dialogue_judge_prompt).resolve()
    target_prompt = _load_prompt(prompt_path)
    simulator_prompt = _load_prompt(
        client_prompt_path, placeholder=SIMULATION_CARD_PLACEHOLDER
    )
    judge_prompt = _load_prompt(judge_prompt_path)
    persona_path = resolve_repo_path(suite["persona_dataset"], field="persona_dataset")
    conversation_path = resolve_repo_path(
        suite["conversation_dataset"], field="conversation_dataset"
    )
    personas = load_personas(persona_path)
    cases = load_conversation_cases(conversation_path)
    if args.limit:
        cases = cases[: args.limit]
    settings = EvalSettings(
        openrouter_api_key=api_key,
        openrouter_model=target_model,
        followup_text="",
        public_base_url=os.getenv("PUBLIC_BASE_URL"),
    )
    client = OpenRouterClient(settings)  # type: ignore[arg-type]
    requirements = requirement_index(suite, active_only=True)
    dialogues = asyncio.run(
        run_dialogues(
            cases,
            personas=personas,
            client=client,
            target_prompt=target_prompt,
            simulator_model=simulator_model,
            simulator_prompt=simulator_prompt,
            simulator_temperature=args.simulator_temperature,
            judge_model=judge_model,
            judge_prompt=judge_prompt,
            requirements=requirements,
            max_concurrent=args.max_concurrent,
            rollouts=args.rollouts,
        )
    )
    run = {
        "schema_version": "sales-dialogue-eval-run-v1",
        "run_id": run_id,
        "generated_at": _utc_now(),
        "git_commit": _git_commit(),
        "suite": validation,
        "inputs": {
            "suite": _file_metadata(suite_path),
            "personas": _file_metadata(persona_path),
            "conversations": _file_metadata(conversation_path),
        },
        "prompts": {
            "target": {
                "path": str(prompt_path),
                "sha256": _sha256(target_prompt),
            },
            "simulated_customer": {
                "path": str(client_prompt_path),
                "sha256": _sha256(simulator_prompt),
            },
            "dialogue_judge": {
                "path": str(judge_prompt_path),
                "sha256": _sha256(judge_prompt),
            },
        },
        "target_model": target_model,
        "simulator_model": simulator_model,
        "simulator_temperature": args.simulator_temperature,
        "judge_model": judge_model,
        "rollouts_per_case": args.rollouts,
        "selection": {
            "limit": args.limit,
            "selected_cases": [case["case_id"] for case in cases],
        },
        "summary": build_dialogue_summary(dialogues),
        "dialogues": dialogues,
    }
    gating_eligible = args.limit is None and not args.skip_judge and args.rollouts >= 3
    if not gating_eligible:
        run["summary"]["release_gate_evaluable"] = False
        run["summary"]["release_gate_passed"] = False
    run["summary"]["gating_eligible"] = gating_eligible
    _write_json(run_path, run)
    examples_path.parent.mkdir(parents=True, exist_ok=True)
    examples_path.write_text(
        "".join(
            json.dumps(
                {
                    "case_id": dialogue["case_id"],
                    "persona_id": dialogue["persona_id"],
                    "rollout": dialogue["rollout"],
                    "provenance": dialogue["provenance"],
                    "transcript": dialogue["transcript"],
                },
                ensure_ascii=False,
            )
            + "\n"
            for dialogue in dialogues
        ),
        encoding="utf-8",
    )
    _write_markdown_report(run, report_path)
    print(
        json.dumps(
            {
                "dialogue_run": str(run_path),
                "dialogue_examples": str(examples_path),
                "dialogue_report": str(report_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if gating_eligible and not run["summary"]["release_gate_passed"]:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run multi-turn sales evals with an LLM-simulated customer"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate", help="validate requirement traceability and dataset coverage"
    )
    validate_parser.add_argument("--suite", default=str(DEFAULT_SUITE))
    validate_parser.set_defaults(handler=validate_command)

    run_parser = subparsers.add_parser("run", help="simulate full customer dialogues")
    run_parser.add_argument("--suite", default=str(DEFAULT_SUITE))
    run_parser.add_argument("--prompt", required=True)
    run_parser.add_argument("--run-id", required=True)
    run_parser.add_argument("--client-prompt", default=str(DEFAULT_CLIENT_PROMPT))
    run_parser.add_argument(
        "--dialogue-judge-prompt", default=str(DEFAULT_JUDGE_PROMPT)
    )
    run_parser.add_argument("--output-dir", default=str(DEFAULT_RESULTS_DIR))
    run_parser.add_argument("--target-model")
    run_parser.add_argument("--simulator-model")
    run_parser.add_argument("--judge-model")
    run_parser.add_argument("--simulator-temperature", type=float, default=0.2)
    run_parser.add_argument("--max-concurrent", type=int, default=2)
    run_parser.add_argument("--rollouts", type=int, default=1)
    run_parser.add_argument("--limit", type=int)
    run_parser.add_argument("--skip-judge", action="store_true")
    run_parser.set_defaults(handler=run_command)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args))
    except RegressionSuiteValidationError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
