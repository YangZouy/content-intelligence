"""
自动化测试：构造一份完整的正常文章状态，包含Front Matter、代码块、公式、图片及 OSS 映射。然后故意制造异常，确认检查器能准确发现问题。
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from src.nodes.quality_check import quality_check_node, run_quality_checks


BODY = """# Reliable workflow

```python
print("hello")
```

The formula is $E=mc^2$.

$$
a^2 + b^2 = c^2
$$

![diagram](https://cdn.example.com/article/diagram.png)
"""


def _document(body: str = BODY, *, cover: str = "https://cdn.example.com/cover.jpg") -> str:
    return f"""---
title: Reliable workflow
date: '2026-08-11 10:00:00'
tags:
- LangGraph
- workflow
- publishing
categories:
- Technology
layout: post
top_img: {cover}
cover: {cover}
description: A reliable publishing workflow.
---

{body}"""


@pytest.fixture
def valid_state():
    document = _document()
    return {
        "title": "Reliable workflow",
        "summary": "A reliable publishing workflow.",
        "tags": ["LangGraph", "workflow", "publishing"],
        "raw_content": BODY,
        "formatted_content": BODY,
        "content_with_oss_images": BODY,
        "images": [{"url_or_path": "diagram.png", "alt": "diagram", "usage": "inline"}],
        "image_mapping": {"diagram.png": "https://cdn.example.com/article/diagram.png"},
        "requested_platforms": ["blog", "wechat"],
        "hexo_document": document,
        "wechat_draft": document,
    }


def _codes(state):
    return {item["code"] for item in run_quality_checks(state)}


def test_valid_documents_pass_all_deterministic_checks(valid_state):
    assert run_quality_checks(valid_state) == []


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("title", "", "metadata.title_missing"),
        ("title", "x" * 31, "metadata.title_too_long"),
        ("summary", "x" * 201, "metadata.summary_too_long"),
        ("tags", ["one", "two"], "metadata.tags_count"),
        ("tags", ["one", "two", "x" * 21], "metadata.tag_invalid"),
    ],
)
def test_metadata_constraints(valid_state, field, value, expected):
    valid_state[field] = value
    assert expected in _codes(valid_state)


def test_missing_front_matter_field_is_reported(valid_state):
    valid_state["hexo_document"] = valid_state["hexo_document"].replace(
        "description: A reliable publishing workflow.\n", ""
    )
    issues = run_quality_checks(valid_state)
    assert any(
        item["code"] == "front_matter.missing_field"
        and item["field"] == "description"
        and item["platform"] == "blog"
        for item in issues
    )


def test_invalid_yaml_front_matter_is_reported(valid_state):
    valid_state["wechat_draft"] = valid_state["wechat_draft"].replace(
        "tags:\n", "tags: [broken\n"
    )
    assert "front_matter.invalid" in _codes(valid_state)


def test_changed_code_block_and_formula_are_reported(valid_state):
    changed = valid_state["hexo_document"].replace('print("hello")', 'print("changed")')
    changed = changed.replace("a^2 + b^2 = c^2", "a + b = c")
    valid_state["hexo_document"] = changed
    codes = _codes(valid_state)
    assert "structure.code_block_lost" in codes
    assert "structure.formula_lost" in codes


def test_unclosed_markdown_structures_are_reported(valid_state):
    valid_state["wechat_draft"] += "\n```python\nprint('broken')\n\n$$\nx + y\n"
    codes = _codes(valid_state)
    assert "markdown.unclosed_code_fence" in codes
    assert "markdown.unclosed_math_block" in codes


def test_incomplete_oss_upload_and_unresolved_image_are_reported(valid_state):
    valid_state["image_mapping"] = {}
    codes = _codes(valid_state)
    assert "oss.upload_incomplete" in codes
    assert "oss.unresolved_image" in codes


def test_missing_mapped_url_is_reported(valid_state):
    valid_state["image_mapping"] = {
        "diagram.png": "https://cdn.example.com/article/not-present.png"
    }
    codes = _codes(valid_state)
    assert "oss.unresolved_image" in codes
    assert "oss.replacement_missing" in codes


def test_only_requested_platform_is_checked(valid_state):
    valid_state["requested_platforms"] = ["blog"]
    valid_state["wechat_draft"] = ""
    assert run_quality_checks(valid_state) == []


def test_node_records_result_and_check_count(valid_state):
    first = quality_check_node(valid_state)
    second_state = deepcopy(valid_state)
    second_state["quality_check_count"] = first["quality_check_count"]
    second = quality_check_node(second_state)

    assert first == {
        "quality_passed": True,
        "quality_issues": [],
        "quality_check_count": 1,
    }
    assert second["quality_check_count"] == 2
