"""Offline benchmark runner for the deterministic content quality baseline."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List

import yaml

from src.nodes.format_optimize import format_optimize_node
from src.nodes.quality_check import (
    _extract_code_blocks,
    _extract_formulas,
    _extract_images,
    run_quality_checks,
)


@dataclass(frozen=True)
class SampleCase:
    id: str
    file: Path
    category: str
    expected_quality_pass: bool


def load_cases(
    samples_dir: Path | str = Path("eval/samples"),
    manifest_path: Path | str = Path("eval/manifest.yaml"),
) -> List[SampleCase]:
    samples_dir = Path(samples_dir)
    manifest = yaml.safe_load(Path(manifest_path).read_text(encoding="utf-8")) or []
    return [
        SampleCase(
            id=item["id"],
            file=samples_dir / item["file"],
            category=item["category"],
            expected_quality_pass=bool(item["expected_quality_pass"]),
        )
        for item in manifest
    ]


def _count_tables(content: str) -> int:
    return sum(1 for line in content.splitlines() if re.match(r"^\s*\|.*\|\s*$", line)) // 2


def _retention(raw: str, formatted: str) -> Dict[str, float]:
    structures = {
        "images": _extract_images,
        "code_blocks": _extract_code_blocks,
        "formulas": _extract_formulas,
        "tables": lambda value: [str(_count_tables(value))] if _count_tables(value) else [],
    }
    rates: Dict[str, float] = {}
    for name, extractor in structures.items():
        source_count = len(extractor(raw))
        target_count = len(extractor(formatted))
        rates[name] = 1.0 if source_count == 0 else min(target_count / source_count, 1.0)
    rates["overall"] = sum(rates.values()) / len(rates)
    return rates


def _metadata_from_source(raw: str) -> Dict[str, Any]:
    title_match = re.search(r"^#\s+(.+)$", raw, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else ""
    return {
        "title": title,
        "summary": raw.replace("\n", " ").strip()[:200],
        "tags": ["evaluation", "markdown", "workflow"],
    }


def evaluate_case(case: SampleCase) -> Dict[str, Any]:
    raw = case.file.read_text(encoding="utf-8")
    formatted = format_optimize_node({"raw_content": raw})["formatted_content"]
    metadata = _metadata_from_source(raw)
    images = [{"url_or_path": image, "usage": "inline"} for image in _extract_images(raw)]
    state = {
        **metadata,
        "raw_content": raw,
        "formatted_content": formatted,
        "content_with_oss_images": formatted,
        "images": images,
        "image_mapping": {},
        "requested_platforms": ["blog"],
        "hexo_document": raw,
        "wechat_draft": raw,
    }
    issues = run_quality_checks(state)
    metadata_success = bool(metadata["title"] and 3 <= len(metadata["tags"]) <= 6)
    return {
        "id": case.id,
        "category": case.category,
        "quality_passed": not issues,
        "expected_quality_pass": case.expected_quality_pass,
        "quality_matches_expectation": (not issues) == case.expected_quality_pass,
        "metadata_structured": metadata_success,
        "structure_retention": _retention(raw, formatted),
        "quality_issue_codes": [issue["code"] for issue in issues],
    }


def run_evaluation(
    samples_dir: Path | str = Path("eval/samples"),
    manifest_path: Path | str = Path("eval/manifest.yaml"),
) -> Dict[str, Any]:
    cases = [evaluate_case(case) for case in load_cases(samples_dir, manifest_path)]
    return {
        "sample_count": len(cases),
        "format_structure_retention_rate": round(
            sum(item["structure_retention"]["overall"] for item in cases) / len(cases), 4
        ) if cases else 0.0,
        "metadata_structured_success_rate": round(
            sum(item["metadata_structured"] for item in cases) / len(cases), 4
        ) if cases else 0.0,
        "quality_first_pass_rate": round(
            sum(item["quality_passed"] for item in cases) / len(cases), 4
        ) if cases else 0.0,
        "quality_expectation_match_rate": round(
            sum(item["quality_matches_expectation"] for item in cases) / len(cases), 4
        ) if cases else 0.0,
        "cases": cases,
    }


def write_evaluation_report(report: Dict[str, Any], output_path: Path | str) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output
