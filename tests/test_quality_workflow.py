from __future__ import annotations

from pathlib import Path

import pytest

import src.graph as graph_module
from src.graph import PipelineGraph, route_after_quality_check
from src.nodes.quality_repair import quality_repair_node
from src.nodes.summary_meta import _build_user_message


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"quality_status": "passed"}, "publish"),
        ({"quality_status": "needs_repair", "quality_repair_count": 0}, "repair"),
        ({"quality_status": "failed", "quality_repair_count": 1}, "stop"),
        ({"quality_status": "needs_repair", "quality_repair_count": 1}, "stop"),
    ],
)
def test_quality_router_never_repeats_repair(state, expected):
    assert route_after_quality_check(state) == expected


def test_repair_node_builds_feedback_and_increments_once():
    result = quality_repair_node({
        "quality_repair_count": 0,
        "quality_issues": [
            {
                "code": "metadata.summary_too_long",
                "message": "summary must not exceed 200 characters",
            },
            {
                "code": "front_matter.missing_field",
                "message": "required field is missing",
            },
        ],
    })

    assert result["quality_repair_count"] == 1
    assert result["quality_status"] == "pending"
    assert "metadata.summary_too_long" in result["quality_feedback"]
    assert "front_matter.missing_field" in result["quality_feedback"]


def test_repair_node_rejects_second_invocation():
    with pytest.raises(RuntimeError, match="repair limit exceeded"):
        quality_repair_node({"quality_repair_count": 1})


def test_regeneration_prompt_contains_quality_feedback():
    message = _build_user_message(
        "# Article\n\nBody",
        "Article",
        "- metadata.tags_count: tags must contain 3 to 6 items",
    )

    assert "一次且仅一次的质量修复" in message
    assert "metadata.tags_count" in message
    assert "不要改写代码、公式、图片或正文事实" in message


def test_overlong_h1_is_not_forced_into_regeneration_prompt():
    title = "x" * 31
    message = _build_user_message("# " + title, title)
    assert "原文已有合规的 H1 标题" not in message


def test_compiled_graph_contains_bounded_quality_branch():
    drawable = PipelineGraph()._graph.get_graph()
    edges = {(edge.source, edge.target) for edge in drawable.edges}

    assert ("content_adapt", "quality_check") in edges
    assert ("quality_check", "approval") in edges
    assert ("approval", "publish_blog") in edges
    assert ("publish_blog", "publish_wechat") in edges
    assert ("quality_check", "quality_repair") in edges
    assert ("quality_repair", "summary_meta") in edges
    assert ("content_adapt", "publish") not in edges


def test_second_quality_failure_stops_before_publish(monkeypatch):
    publish_called = False

    monkeypatch.setattr(graph_module, "ingest_node", lambda state: {
        "raw_content": "# Article\n\nBody",
        "images": [],
    })
    monkeypatch.setattr(graph_module, "format_optimize_node", lambda state: {
        "formatted_content": state["raw_content"],
    })
    monkeypatch.setattr(graph_module, "article_identity_node", lambda state: {
        "article_id": "article_test",
        "publication_date": "2026-08-12",
    })
    monkeypatch.setattr(graph_module, "summary_meta_node", lambda state: {
        "title": "Article",
        "summary": "",
        "tags": ["one", "two", "three"],
    })
    monkeypatch.setattr(graph_module, "image_process_node", lambda state: {
        "content_with_oss_images": state["formatted_content"],
        "image_mapping": {},
        "oss_image_count": 0,
    })
    monkeypatch.setattr(graph_module, "cover_image_node", lambda state: {
        "cover_url": "https://cdn.example.com/cover.jpg",
    })

    def adapt(state):
        document = """---
title: Article
date: '2026-08-12'
tags: [one, two, three]
categories: [Technology]
layout: post
cover: https://cdn.example.com/cover.jpg
description: placeholder
---

# Article

Body"""
        return {"hexo_document": document, "wechat_draft": document, "content_version": "version_test"}

    def publish(state):
        nonlocal publish_called
        publish_called = True
        return {"publish_results": []}

    monkeypatch.setattr(graph_module, "content_adapt_node", adapt)
    monkeypatch.setattr(graph_module, "publish_blog_node", publish)
    monkeypatch.setattr(graph_module, "publish_wechat_node", publish)

    final_state = PipelineGraph(Path(":memory:"))._graph.invoke(
        {
                "requested_platforms": ["blog"],
                "file_path": "article.md",
            "quality_repair_count": 0,
        },
        config={"configurable": {"thread_id": "quality-failure-test"}},
    )

    assert final_state["quality_status"] == "failed"
    assert final_state["quality_check_count"] == 2
    assert final_state["quality_repair_count"] == 1
    assert publish_called is False
