from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
SUITE_SCHEMA_VERSION = "sales-regression-suite-v1"
REQUIREMENT_STATUSES = {"active", "superseded", "conflict", "separate_suite"}
EVAL_LAYERS = {"checkpoint", "dialogue"}
FACT_CONFIDENCE = {"observed", "ambiguous", "unknown"}
SENSITIVE_KEYS = {
    "chat_id",
    "name",
    "telegram_user_id",
    "user_record_id",
    "username",
}
TELEGRAM_LINK_RE = re.compile(r"(?:https?://)?t\.me/|@[A-Za-z][A-Za-z0-9_]{4,}")


class RegressionSuiteValidationError(ValueError):
    pass


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RegressionSuiteValidationError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RegressionSuiteValidationError(f"{path}: expected a JSON object")
    return payload


def resolve_repo_path(value: str, *, field: str) -> Path:
    path = (ROOT_DIR / value).resolve()
    try:
        path.relative_to(ROOT_DIR)
    except ValueError as exc:
        raise RegressionSuiteValidationError(
            f"{field} must point inside the repository: {value!r}"
        ) from exc
    if not path.is_file():
        raise RegressionSuiteValidationError(f"{field} does not exist: {path}")
    return path


def load_jsonl(path: Path, id_field: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise RegressionSuiteValidationError(
                f"{path}:{line_number}: invalid JSON: {exc}"
            ) from exc
        if not isinstance(row, dict):
            raise RegressionSuiteValidationError(
                f"{path}:{line_number}: expected a JSON object"
            )
        value = row.get(id_field)
        if not isinstance(value, str) or not value.strip():
            raise RegressionSuiteValidationError(
                f"{path}:{line_number}: missing non-empty {id_field}"
            )
        if value in seen_ids:
            raise RegressionSuiteValidationError(
                f"{path}:{line_number}: duplicate {id_field} {value!r}"
            )
        row["_source_line"] = line_number
        rows.append(row)
        seen_ids.add(value)
    if not rows:
        raise RegressionSuiteValidationError(f"{path}: dataset is empty")
    return rows


def _require_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegressionSuiteValidationError(f"{field} must be a non-empty string")
    return value


def _require_string_list(value: Any, *, field: str, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        suffix = "" if allow_empty else " non-empty"
        raise RegressionSuiteValidationError(f"{field} must be a{suffix} list")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise RegressionSuiteValidationError(f"{field} must contain non-empty strings")
    if len(value) != len(set(value)):
        raise RegressionSuiteValidationError(f"{field} contains duplicates")
    return value


def _validate_requirement_references(
    rows: list[dict[str, Any]],
    *,
    dataset_name: str,
    id_field: str,
    active_requirement_ids: set[str],
) -> dict[str, set[str]]:
    coverage: dict[str, set[str]] = {}
    for row in rows:
        row_id = str(row[id_field])
        requirement_ids = _require_string_list(
            row.get("requirement_ids"),
            field=f"{dataset_name} {row_id}.requirement_ids",
        )
        unknown = set(requirement_ids) - active_requirement_ids
        if unknown:
            raise RegressionSuiteValidationError(
                f"{dataset_name} {row_id} references inactive or unknown requirement(s): "
                + ", ".join(sorted(unknown))
            )
        for requirement_id in requirement_ids:
            coverage.setdefault(requirement_id, set()).add(row_id)
    return coverage


def _validate_checkpoint_mapping(
    rows: list[dict[str, Any]],
    *,
    mapping: Any,
    active_requirement_ids: set[str],
) -> dict[str, set[str]]:
    if not isinstance(mapping, dict):
        raise RegressionSuiteValidationError(
            "checkpoint_case_requirements must be an object"
        )
    case_ids = {str(row["case_id"]) for row in rows}
    mapped_case_ids = set(mapping)
    missing = case_ids - mapped_case_ids
    unknown = mapped_case_ids - case_ids
    if missing:
        raise RegressionSuiteValidationError(
            "checkpoint case(s) missing requirement mapping: "
            + ", ".join(sorted(missing))
        )
    if unknown:
        raise RegressionSuiteValidationError(
            "checkpoint_case_requirements references unknown case(s): "
            + ", ".join(sorted(unknown))
        )
    coverage: dict[str, set[str]] = {}
    for case_id, requirement_ids_value in mapping.items():
        requirement_ids = _require_string_list(
            requirement_ids_value,
            field=f"checkpoint_case_requirements.{case_id}",
        )
        invalid = set(requirement_ids) - active_requirement_ids
        if invalid:
            raise RegressionSuiteValidationError(
                f"checkpoint {case_id} references inactive or unknown requirement(s): "
                + ", ".join(sorted(invalid))
            )
        for requirement_id in requirement_ids:
            coverage.setdefault(requirement_id, set()).add(case_id)
    return coverage


def _walk_privacy(value: Any, *, field: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in SENSITIVE_KEYS:
                raise RegressionSuiteValidationError(
                    f"{field} contains forbidden identifying key {key!r}"
                )
            _walk_privacy(child, field=f"{field}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_privacy(child, field=f"{field}[{index}]")
    elif isinstance(value, str) and TELEGRAM_LINK_RE.search(value):
        raise RegressionSuiteValidationError(
            f"{field} appears to contain a Telegram username or link"
        )


def load_regression_suite(path: Path) -> dict[str, Any]:
    suite = _read_object(path)
    if suite.get("schema_version") != SUITE_SCHEMA_VERSION:
        raise RegressionSuiteValidationError(
            f"{path}: unsupported schema_version {suite.get('schema_version')!r}"
        )
    _require_string(suite.get("suite_id"), field=f"{path}.suite_id")
    for field in ("checkpoint_dataset", "persona_dataset", "conversation_dataset"):
        _require_string(suite.get(field), field=f"{path}.{field}")
    if not isinstance(suite.get("checkpoint_case_requirements"), dict):
        raise RegressionSuiteValidationError(
            f"{path}.checkpoint_case_requirements must be an object"
        )
    requirements = suite.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise RegressionSuiteValidationError(f"{path}.requirements must be a non-empty list")
    return suite


def _validate_requirements(suite: dict[str, Any]) -> dict[str, dict[str, Any]]:
    requirements: dict[str, dict[str, Any]] = {}
    for index, requirement in enumerate(suite["requirements"]):
        field = f"requirements[{index}]"
        if not isinstance(requirement, dict):
            raise RegressionSuiteValidationError(f"{field} must be an object")
        requirement_id = _require_string(
            requirement.get("requirement_id"), field=f"{field}.requirement_id"
        )
        if requirement_id in requirements:
            raise RegressionSuiteValidationError(
                f"duplicate requirement_id {requirement_id!r}"
            )
        status = requirement.get("status")
        if status not in REQUIREMENT_STATUSES:
            raise RegressionSuiteValidationError(
                f"{requirement_id}.status must be one of {sorted(REQUIREMENT_STATUSES)}"
            )
        for required_field in (
            "title",
            "rationale",
            "introduced_in",
            "evaluation_level",
            "evaluator",
        ):
            _require_string(
                requirement.get(required_field),
                field=f"{requirement_id}.{required_field}",
            )
        _require_string_list(
            requirement.get("criteria"), field=f"{requirement_id}.criteria"
        )
        sources = requirement.get("sources")
        if not isinstance(sources, list) or not sources:
            raise RegressionSuiteValidationError(
                f"{requirement_id}.sources must be a non-empty list"
            )
        for source_index, source in enumerate(sources):
            if not isinstance(source, dict):
                raise RegressionSuiteValidationError(
                    f"{requirement_id}.sources[{source_index}] must be an object"
                )
            _require_string(
                source.get("kind"),
                field=f"{requirement_id}.sources[{source_index}].kind",
            )
            _require_string(
                source.get("reference"),
                field=f"{requirement_id}.sources[{source_index}].reference",
            )
        required_layers = _require_string_list(
            requirement.get("required_layers", []),
            field=f"{requirement_id}.required_layers",
            allow_empty=status != "active",
        )
        invalid_layers = set(required_layers) - EVAL_LAYERS
        if invalid_layers:
            raise RegressionSuiteValidationError(
                f"{requirement_id} has unsupported layer(s): "
                + ", ".join(sorted(invalid_layers))
            )
        if status != "active" and required_layers:
            raise RegressionSuiteValidationError(
                f"{requirement_id} is {status} and cannot be a release gate"
            )
        requirements[requirement_id] = requirement
    return requirements


def _validate_personas(rows: list[dict[str, Any]], *, path: Path) -> dict[str, dict[str, Any]]:
    required_fields = {
        "persona_id",
        "provenance",
        "summary",
        "style",
        "facts",
        "response_policy",
        "offer_behavior",
        "exit_policy",
    }
    personas: dict[str, dict[str, Any]] = {}
    for row in rows:
        persona_id = str(row["persona_id"])
        missing = required_fields - set(row)
        if missing:
            raise RegressionSuiteValidationError(
                f"{path}:{row['_source_line']}: {persona_id} missing fields: "
                + ", ".join(sorted(missing))
            )
        _walk_privacy(row, field=f"persona {persona_id}")
        provenance = row["provenance"]
        if not isinstance(provenance, dict):
            raise RegressionSuiteValidationError(f"{persona_id}.provenance must be an object")
        _require_string(
            provenance.get("dialogue_id"), field=f"{persona_id}.provenance.dialogue_id"
        )
        _require_string_list(
            provenance.get("message_ids"), field=f"{persona_id}.provenance.message_ids"
        )
        for field in ("style", "offer_behavior", "exit_policy"):
            if not isinstance(row[field], dict):
                raise RegressionSuiteValidationError(f"{persona_id}.{field} must be an object")
        _require_string_list(
            row["response_policy"], field=f"{persona_id}.response_policy"
        )
        facts = row["facts"]
        if not isinstance(facts, list) or not facts:
            raise RegressionSuiteValidationError(f"{persona_id}.facts must be a non-empty list")
        fact_ids: set[str] = set()
        for fact_index, fact in enumerate(facts):
            if not isinstance(fact, dict):
                raise RegressionSuiteValidationError(
                    f"{persona_id}.facts[{fact_index}] must be an object"
                )
            fact_id = _require_string(
                fact.get("fact_id"), field=f"{persona_id}.facts[{fact_index}].fact_id"
            )
            if fact_id in fact_ids:
                raise RegressionSuiteValidationError(
                    f"{persona_id} has duplicate fact_id {fact_id!r}"
                )
            if fact.get("confidence") not in FACT_CONFIDENCE:
                raise RegressionSuiteValidationError(
                    f"{persona_id}.{fact_id}.confidence must be one of "
                    f"{sorted(FACT_CONFIDENCE)}"
                )
            _require_string_list(
                fact.get("reveal_when"),
                field=f"{persona_id}.{fact_id}.reveal_when",
            )
            fact_ids.add(fact_id)
        row["_fact_ids"] = sorted(fact_ids)
        personas[persona_id] = row
    return personas


def _validate_conversations(
    rows: list[dict[str, Any]],
    *,
    path: Path,
    personas: dict[str, dict[str, Any]],
) -> None:
    required_fields = {
        "case_id",
        "persona_id",
        "initial_bot_message",
        "initial_user_message",
        "initial_revealed_fact_ids",
        "max_assistant_turns",
        "expected",
        "trajectory_invariants",
        "requirement_ids",
    }
    used_personas: set[str] = set()
    for row in rows:
        case_id = str(row["case_id"])
        missing = required_fields - set(row)
        if missing:
            raise RegressionSuiteValidationError(
                f"{path}:{row['_source_line']}: {case_id} missing fields: "
                + ", ".join(sorted(missing))
            )
        persona_id = _require_string(row.get("persona_id"), field=f"{case_id}.persona_id")
        if persona_id not in personas:
            raise RegressionSuiteValidationError(
                f"{case_id} references unknown persona {persona_id!r}"
            )
        for field in ("initial_bot_message", "initial_user_message"):
            _require_string(row.get(field), field=f"{case_id}.{field}")
        initial_fact_ids = _require_string_list(
            row.get("initial_revealed_fact_ids"),
            field=f"{case_id}.initial_revealed_fact_ids",
            allow_empty=True,
        )
        unknown_facts = set(initial_fact_ids) - set(personas[persona_id]["_fact_ids"])
        if unknown_facts:
            raise RegressionSuiteValidationError(
                f"{case_id} starts with unknown persona fact(s): "
                + ", ".join(sorted(unknown_facts))
            )
        max_turns = row.get("max_assistant_turns")
        if not isinstance(max_turns, int) or not 1 <= max_turns <= 20:
            raise RegressionSuiteValidationError(
                f"{case_id}.max_assistant_turns must be from 1 to 20"
            )
        expected = row.get("expected")
        if not isinstance(expected, dict):
            raise RegressionSuiteValidationError(f"{case_id}.expected must be an object")
        if expected.get("lead_status") not in {"target", "non_target", "unknown"}:
            raise RegressionSuiteValidationError(
                f"{case_id}.expected.lead_status is invalid"
            )
        if expected.get("button_outcome") not in {
            "eventual_button",
            "never_button",
            "not_required",
        }:
            raise RegressionSuiteValidationError(
                f"{case_id}.expected.button_outcome is invalid"
            )
        _require_string(
            expected.get("terminal_goal"), field=f"{case_id}.expected.terminal_goal"
        )
        minimum_turns = expected.get("minimum_assistant_turns")
        if not isinstance(minimum_turns, int) or not 1 <= minimum_turns <= max_turns:
            raise RegressionSuiteValidationError(
                f"{case_id}.expected.minimum_assistant_turns must be from 1 to "
                "max_assistant_turns"
            )
        earliest_button_turn = expected.get("earliest_button_turn")
        if expected["button_outcome"] == "eventual_button":
            if (
                not isinstance(earliest_button_turn, int)
                or not 1 <= earliest_button_turn <= max_turns
            ):
                raise RegressionSuiteValidationError(
                    f"{case_id}.expected.earliest_button_turn must be from 1 to "
                    "max_assistant_turns for eventual_button"
                )
        elif earliest_button_turn is not None:
            raise RegressionSuiteValidationError(
                f"{case_id}.expected.earliest_button_turn only applies to "
                "eventual_button"
            )
        invariants = row.get("trajectory_invariants")
        if not isinstance(invariants, list) or not invariants:
            raise RegressionSuiteValidationError(
                f"{case_id}.trajectory_invariants must be a non-empty list"
            )
        for invariant_index, invariant in enumerate(invariants):
            if not isinstance(invariant, dict):
                raise RegressionSuiteValidationError(
                    f"{case_id}.trajectory_invariants[{invariant_index}] must be an object"
                )
            for field in ("invariant_id", "criterion", "severity"):
                _require_string(
                    invariant.get(field),
                    field=f"{case_id}.trajectory_invariants[{invariant_index}].{field}",
                )
            if invariant.get("severity") not in {"critical", "major"}:
                raise RegressionSuiteValidationError(
                    f"{case_id}.trajectory_invariants[{invariant_index}].severity is invalid"
                )
        used_personas.add(persona_id)
    unused_personas = set(personas) - used_personas
    if unused_personas:
        raise RegressionSuiteValidationError(
            "persona(s) without a conversation case: " + ", ".join(sorted(unused_personas))
        )


def validate_regression_suite(path: Path) -> dict[str, Any]:
    suite = load_regression_suite(path)
    requirements = _validate_requirements(suite)
    active_requirement_ids = {
        requirement_id
        for requirement_id, requirement in requirements.items()
        if requirement["status"] == "active"
    }
    checkpoint_path = resolve_repo_path(
        suite["checkpoint_dataset"], field="checkpoint_dataset"
    )
    persona_path = resolve_repo_path(suite["persona_dataset"], field="persona_dataset")
    conversation_path = resolve_repo_path(
        suite["conversation_dataset"], field="conversation_dataset"
    )
    checkpoint_rows = load_jsonl(checkpoint_path, "case_id")
    persona_rows = load_jsonl(persona_path, "persona_id")
    conversation_rows = load_jsonl(conversation_path, "case_id")
    personas = _validate_personas(persona_rows, path=persona_path)
    _validate_conversations(conversation_rows, path=conversation_path, personas=personas)
    checkpoint_coverage = _validate_checkpoint_mapping(
        checkpoint_rows,
        mapping=suite["checkpoint_case_requirements"],
        active_requirement_ids=active_requirement_ids,
    )
    dialogue_coverage = _validate_requirement_references(
        conversation_rows,
        dataset_name="dialogue",
        id_field="case_id",
        active_requirement_ids=active_requirement_ids,
    )
    missing_coverage: list[str] = []
    for requirement_id in sorted(active_requirement_ids):
        required_layers = set(requirements[requirement_id]["required_layers"])
        if "checkpoint" in required_layers and requirement_id not in checkpoint_coverage:
            missing_coverage.append(f"{requirement_id}:checkpoint")
        if "dialogue" in required_layers and requirement_id not in dialogue_coverage:
            missing_coverage.append(f"{requirement_id}:dialogue")
    if missing_coverage:
        raise RegressionSuiteValidationError(
            "active requirements missing release-gate coverage: "
            + ", ".join(missing_coverage)
        )

    return {
        "suite_id": suite["suite_id"],
        "suite_path": str(path.resolve()),
        "checkpoint_dataset": str(checkpoint_path),
        "persona_dataset": str(persona_path),
        "conversation_dataset": str(conversation_path),
        "requirements": len(requirements),
        "active_requirements": len(active_requirement_ids),
        "superseded_requirements": sum(
            item["status"] == "superseded" for item in requirements.values()
        ),
        "conflict_requirements": sum(
            item["status"] == "conflict" for item in requirements.values()
        ),
        "checkpoint_cases": len(checkpoint_rows),
        "dialogue_cases": len(conversation_rows),
        "personas": len(personas),
        "checkpoint_coverage": {
            key: sorted(value) for key, value in sorted(checkpoint_coverage.items())
        },
        "dialogue_coverage": {
            key: sorted(value) for key, value in sorted(dialogue_coverage.items())
        },
    }


def requirement_index(suite: dict[str, Any], *, active_only: bool = False) -> dict[str, dict[str, Any]]:
    return {
        str(item["requirement_id"]): item
        for item in suite["requirements"]
        if isinstance(item, dict)
        and isinstance(item.get("requirement_id"), str)
        and (not active_only or item.get("status") == "active")
    }
