import json

import pytest

from evals.reporting import (
    build_alignment_report,
    build_prompt_improvement_packet,
    merge_human_feedback,
    process_human_feedback,
    render_business_review_html,
    render_technical_report_html,
    write_business_review_html,
    write_json_artifact,
    write_technical_report_html,
)


@pytest.fixture
def eval_run() -> dict:
    return {
        "schema_version": "sales-eval-run-v1",
        "run_id": "baseline-v0",
        "generated_at": "2026-07-10T12:00:00Z",
        "prompt": {"path": "prompts/user_chat.system.md", "sha256": "abc123"},
        "target_model": "private-target-model",
        "judge_model": "private-judge-model",
        "dataset_version": "sales-v1",
        "summary": {"passed": 1, "failed": 1},
        "cases": [
            {
                "case_id": "good-case",
                "family": "discovery",
                "scope": "turn",
                "transcript": "outgoing: В какой нише проект?",
                "user_message": "Фитнес",
                "expected": {
                    "lead_status": "unknown",
                    "button_action": "hide",
                    "behavior": "Уточнить продукт. SECRET_EXPECTATION",
                    "must_not": ["Предлагать тест-драйв"],
                },
                "assistant_output": {
                    "text": "Что именно продаёшь?",
                    "should_send_offer": False,
                    "button_rendered": False,
                    "source": "model",
                },
                "hard_checks": [
                    {
                        "name": "button_hidden",
                        "passed": True,
                        "expected": False,
                        "actual": False,
                        "reason": "SECRET_HARD_GOOD",
                    }
                ],
                "judge_metrics": [
                    {
                        "name": "sales_quality",
                        "score": 0.9,
                        "threshold": 0.7,
                        "passed": True,
                        "reason": "SECRET_JUDGE_GOOD",
                        "error": None,
                    }
                ],
            },
            {
                "case_id": "bad-case",
                "family": "offer_timing",
                "scope": "conversation",
                "transcript": "incoming: </script><script>alert('xss')</script>",
                "user_message": "Дай ссылку",
                "expected": {
                    "lead_status": "non_target",
                    "button_action": "hide",
                    "behavior": "Не показывать кнопку",
                    "must_not": ["Давать ссылку"],
                },
                "assistant_output": {
                    "text": "Конечно, жми кнопку <img src=x onerror=alert(1)>",
                    "should_send_offer": True,
                    "button_rendered": True,
                    "source": "model",
                },
                "hard_checks": [
                    {
                        "name": "button_hidden",
                        "passed": False,
                        "expected": False,
                        "actual": True,
                        "reason": "Кнопка показана нецелевому лиду",
                    }
                ],
                "judge_metrics": [
                    {
                        "name": "sales_quality",
                        "score": 0.2,
                        "threshold": 0.7,
                        "passed": False,
                        "reason": "Оффер сделан слишком рано",
                        "error": None,
                    }
                ],
            },
        ],
    }


@pytest.fixture
def human_review() -> dict:
    return {
        "schema_version": "human-review-v1",
        "run_id": "baseline-v0",
        "reviewer_id": "client-expert-1",
        "reviews": [
            {
                "case_id": "good-case",
                "lead_status": "not_enough_data",
                "response_acceptable": "yes",
                "button_should_be_shown_now": "no",
                "failure_tags": [],
                "expected_behavior": "",
                "suggested_response": "",
                "expert_note": "Хороший следующий вопрос",
            },
            {
                "case_id": "bad-case",
                "lead_status": "non_target",
                "response_acceptable": "no",
                "button_should_be_shown_now": "no",
                "failure_tags": ["offer_to_non_target", "premature_offer"],
                "expected_behavior": "Не давать кнопку и завершить квалификацию.",
                "suggested_response": "Сейчас этот формат не подойдёт.",
                "expert_note": "Критическая ошибка",
            },
        ],
    }


def test_business_report_is_blind_interactive_and_escapes_content(eval_run: dict) -> None:
    report = render_business_review_html(eval_run)

    assert "Что именно продаёшь?" in report
    assert "Кнопка фактически показана" in report
    assert "localStorage" in report
    assert "Скачать reviewed.json" in report
    assert "human-review-v1" in report
    assert 'JSON.stringify(artifact, null, 2) + "\\n"' in report
    assert 'name="lead_status-1"' in report
    assert 'name="lead_status-2"' in report
    assert "SECRET_EXPECTATION" not in report
    assert "SECRET_HARD_GOOD" not in report
    assert "SECRET_JUDGE_GOOD" not in report
    assert "private-target-model" not in report
    assert "private-judge-model" not in report
    assert "<script>alert('xss')</script>" not in report
    assert "&lt;/script&gt;&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;" in report
    assert "<img src=x onerror=alert(1)>" not in report


def test_technical_report_contains_diagnostics_and_escapes_content(eval_run: dict) -> None:
    report = render_technical_report_html(eval_run)

    assert "private-target-model" in report
    assert "private-judge-model" in report
    assert "SECRET_EXPECTATION" in report
    assert "SECRET_HARD_GOOD" in report
    assert "SECRET_JUDGE_GOOD" in report
    assert "Оффер сделан слишком рано" in report
    assert "<script>alert('xss')</script>" not in report
    assert "&lt;/script&gt;&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;" in report
    assert "<img src=x onerror=alert(1)>" not in report


def test_report_and_json_writers_create_parent_directories(
    tmp_path, eval_run: dict, human_review: dict
) -> None:
    business_path = write_business_review_html(eval_run, tmp_path / "nested" / "business.html")
    technical_path = write_technical_report_html(eval_run, tmp_path / "nested" / "technical.html")
    json_path = write_json_artifact(human_review, tmp_path / "nested" / "reviewed.json")

    assert business_path.is_file()
    assert technical_path.is_file()
    assert json.loads(json_path.read_text(encoding="utf-8")) == human_review


def test_merge_feedback_builds_alignment_by_stable_case_id(
    eval_run: dict, human_review: dict
) -> None:
    human_review["reviews"].reverse()

    merged = merge_human_feedback(eval_run, human_review)
    alignment = merged["alignment"]

    assert merged["cases"][0]["case_id"] == "good-case"
    assert merged["cases"][0]["human_review"]["response_acceptable"] == "yes"
    assert merged["cases"][1]["human_review"]["response_acceptable"] == "no"
    assert merged["cases"][1]["human_review"]["reviewer_id"] == "client-expert-1"
    assert "human_review" not in eval_run["cases"][0]
    assert alignment["response"]["compared"] == 2
    assert alignment["response"]["agreement_rate"] == 1.0
    assert alignment["response"]["confusion"] == {
        "human_good_judge_good": 1,
        "human_good_judge_bad": 0,
        "human_bad_judge_good": 0,
        "human_bad_judge_bad": 1,
    }
    assert alignment["button"]["compared"] == 2
    assert alignment["button"]["agreement_rate"] == 0.5
    assert alignment["button"]["disagreements"][0]["case_id"] == "bad-case"
    assert alignment["lead_status"]["agreement_rate"] == 1.0


def test_merge_allows_partial_reviews(eval_run: dict, human_review: dict) -> None:
    human_review["reviews"][0]["lead_status"] = ""
    human_review["reviews"][0]["response_acceptable"] = ""
    human_review["reviews"][0]["button_should_be_shown_now"] = ""

    merged = merge_human_feedback(eval_run, human_review)
    alignment = merged["alignment"]

    assert merged["cases"][0]["human_review"]["response_acceptable"] == ""
    assert merged["cases"][1]["human_review"]["response_acceptable"] == "no"
    assert alignment["response"]["compared"] == 1
    assert alignment["response"]["agreement_rate"] == 1.0
    assert alignment["button"]["compared"] == 1
    assert alignment["lead_status"]["compared"] == 1


def test_alignment_exposes_too_lenient_judge(eval_run: dict, human_review: dict) -> None:
    eval_run["cases"][1]["judge_metrics"][0]["passed"] = True
    merged = merge_human_feedback(eval_run, human_review)

    alignment = build_alignment_report(merged, judge_metric_name="sales_quality")

    assert alignment["response"]["agreement_rate"] == 0.5
    assert alignment["response"]["confusion"]["human_bad_judge_good"] == 1
    assert alignment["response"]["disagreements"] == [
        {"case_id": "bad-case", "human": "no", "judge": "pass"}
    ]


def test_prompt_packet_is_human_grounded(eval_run: dict, human_review: dict) -> None:
    # A judge-only failure on a human-approved response must not become a prompt target.
    eval_run["cases"][0]["judge_metrics"][0]["passed"] = False
    merged = merge_human_feedback(eval_run, human_review)

    packet = build_prompt_improvement_packet(merged)

    assert packet["schema_version"] == "prompt-improvement-packet-v1"
    assert packet["summary"]["actionable_cases"] == 1
    assert packet["summary"]["human_rejected_responses"] == 1
    assert packet["summary"]["human_button_mismatches"] == 1
    assert packet["summary"]["failed_hard_checks"] == 1
    assert packet["summary"]["failure_tag_counts"] == {
        "offer_to_non_target": 1,
        "premature_offer": 1,
    }
    assert [item["case_id"] for item in packet["items"]] == ["bad-case"]
    assert packet["items"][0]["issues"] == [
        "human_rejected_response",
        "human_button_mismatch",
        "hard_check_failure",
    ]
    assert packet["metric_calibration_warning"]["response_disagreements"] == [
        {"case_id": "good-case", "human": "yes", "judge": "fail"}
    ]


def test_process_human_feedback_returns_all_machine_artifacts(
    eval_run: dict, human_review: dict
) -> None:
    artifacts = process_human_feedback(eval_run, human_review)

    assert set(artifacts) == {"merged_run", "alignment", "prompt_improvement_packet"}
    assert artifacts["alignment"]["schema_version"] == "human-judge-alignment-v1"
    assert artifacts["prompt_improvement_packet"]["run_id"] == "baseline-v0"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda review: review.update(run_id="another-run"), "run_id mismatch"),
        (
            lambda review: review["reviews"][0].update(case_id="unknown-case"),
            "unknown case_id",
        ),
        (
            lambda review: review["reviews"].append(dict(review["reviews"][0])),
            "duplicate case_id",
        ),
        (
            lambda review: review["reviews"][0].update(response_acceptable="maybe"),
            "unsupported value",
        ),
        (
            lambda review: review["reviews"][0].update(failure_tags=["invented_tag"]),
            "unsupported values",
        ),
    ],
)
def test_merge_rejects_invalid_review(
    eval_run: dict, human_review: dict, mutation, message: str
) -> None:
    mutation(human_review)

    with pytest.raises(ValueError, match=message):
        merge_human_feedback(eval_run, human_review)
