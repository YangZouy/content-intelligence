from __future__ import annotations

from pathlib import Path
import uuid

from src.evaluation import evaluate_case, load_cases, run_evaluation


def test_fixed_evaluation_manifest_has_twenty_samples_and_five_categories():
    cases = load_cases()
    assert len(cases) == 20
    assert {case.category for case in cases} == {
        "ordinary",
        "multi_image",
        "code_formula_table",
        "missing_front_matter",
        "broken_links",
    }


def test_evaluation_reports_expected_quality_boundaries():
    report = run_evaluation()
    assert report["sample_count"] == 20
    assert report["quality_expectation_match_rate"] == 1.0
    by_id = {item["id"]: item for item in report["cases"]}
    assert by_id["multi_image"]["structure_retention"]["images"] == 1.0
    assert "front_matter.invalid" in by_id["missing_front_matter"]["quality_issue_codes"]
    assert "oss.upload_incomplete" in by_id["broken_links"]["quality_issue_codes"]


def test_report_can_be_written():
    from src.evaluation import write_evaluation_report

    output = Path("tests/.eval-output") / f"{uuid.uuid4().hex}.json"
    try:
        output = write_evaluation_report(run_evaluation(), output)
        assert output.exists()
        assert '"sample_count": 20' in output.read_text(encoding="utf-8")
    finally:
        output.unlink(missing_ok=True)
        output.parent.rmdir()
