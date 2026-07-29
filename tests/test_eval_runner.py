import json

import pytest

from evals.run_sales_eval import (
    ScenarioValidationError,
    build_hard_checks,
    build_summary,
    load_scenarios,
)


def _scenario(**overrides):
    scenario = {
        "case_id": "case-1",
        "family": "qualification",
        "transcript": "outgoing: В какой нише работаешь?",
        "user_message": "фитнес",
        "lead_status": "target",
        "expected_button_action": "do_not_show",
        "expected_behavior": "Принять нишу и уточнить продукт.",
        "hard_constraints": {},
        "tags": [],
    }
    scenario.update(overrides)
    return scenario


def test_load_scenarios_reads_jsonl_and_sets_defaults(tmp_path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(_scenario(), ensure_ascii=False) + "\n", encoding="utf-8")

    scenarios = load_scenarios(path)

    assert scenarios[0]["scope"] == "turn"
    assert scenarios[0]["case_id"] == "case-1"


def test_load_scenarios_rejects_duplicate_ids(tmp_path) -> None:
    path = tmp_path / "cases.jsonl"
    row = json.dumps(_scenario(), ensure_ascii=False)
    path.write_text(f"{row}\n{row}\n", encoding="utf-8")

    with pytest.raises(ScenarioValidationError, match="duplicate case_id"):
        load_scenarios(path)


def test_hard_checks_reject_offer_to_non_target() -> None:
    checks = build_hard_checks(
        _scenario(lead_status="non_target", expected_button_action="do_not_show"),
        reply_text="Жми кнопку ниже.",
        should_send_offer=True,
        button_rendered=True,
    )
    failures = {check["name"] for check in checks if not check["passed"]}

    assert "offer_decision" in failures
    assert "button_rendering" in failures
    assert "non_target_never_gets_offer" in failures


def test_hard_checks_accept_expected_button_and_clean_reply() -> None:
    checks = build_hard_checks(
        _scenario(expected_button_action="show"),
        reply_text="Да, жми кнопку ниже.",
        should_send_offer=True,
        button_rendered=True,
    )

    assert all(check["passed"] for check in checks)


def test_hard_checks_reject_adjacent_duplicate_words() -> None:
    checks = build_hard_checks(
        _scenario(),
        reply_text="Сначала сначала посмотрим путь от заявки до оплаты.",
        should_send_offer=False,
        button_rendered=False,
    )

    duplicate_check = next(
        check for check in checks if check["name"] == "no_adjacent_duplicate_words"
    )
    assert duplicate_check["passed"] is False


def test_hard_checks_reject_awkward_parallel_numeric_goal() -> None:
    checks = build_hard_checks(
        _scenario(),
        reply_text=(
            "Нужно увеличить заявки с 10 до 30 и оплаты с 5 до 10 в месяц. "
            "Показать условия?"
        ),
        should_send_offer=False,
        button_rendered=False,
    )

    parallel_check = next(
        check
        for check in checks
        if check["name"] == "no_awkward_parallel_numeric_goal"
    )
    assert parallel_check["passed"] is False


def test_hard_checks_enforce_personalized_value_before_price() -> None:
    scenario = _scenario(
        hard_constraints={
            "ordered_mentions": [
                {
                    "before_any": ["разберут", "определить"],
                    "after_any": ["1000", "1 000"],
                }
            ]
        }
    )

    passing = build_hard_checks(
        scenario,
        reply_text=(
            "На тест-драйве разберут путь от заявки до оплаты. "
            "Формат стоит 1000 рублей."
        ),
        should_send_offer=False,
        button_rendered=False,
    )
    failing = build_hard_checks(
        scenario,
        reply_text=(
            "Тест-драйв стоит 1000 рублей. Затем разберут путь от заявки до оплаты."
        ),
        should_send_offer=False,
        button_rendered=False,
    )

    assert next(
        check for check in passing if check["name"] == "ordered_mentions:0"
    )["passed"]
    assert not next(
        check for check in failing if check["name"] == "ordered_mentions:0"
    )["passed"]


def test_build_summary_aggregates_hard_and_judge_results() -> None:
    case = {
        "expected": {"lead_status": "target"},
        "assistant_output": {"error": None, "button_rendered": False},
        "hard_checks": [{"name": "ok", "passed": True}],
        "judge_metrics": [
            {"name": "Sales Next-Step Quality", "score": 0.8, "passed": True}
        ],
    }

    summary = build_summary([case])

    assert summary["hard_passed_cases"] == 1
    assert summary["judge_metrics"]["Sales Next-Step Quality"]["average_score"] == 0.8
