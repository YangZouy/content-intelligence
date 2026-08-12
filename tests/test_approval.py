from __future__ import annotations

from pathlib import Path

import pytest

import src.graph as graph_module
from src.graph import PipelineGraph
from src.nodes.approval import build_approval_preview, route_after_approval


def test_approval_preview_contains_publish_decision_fields():
    preview = build_approval_preview({
        "title": "Article",
        "summary": "Summary",
        "tags": ["one", "two", "three"],
        "cover_url": "https://cdn.example.com/cover.jpg",
        "requested_platforms": ["blog", "wechat"],
        "hexo_document": "large document must not be included",
    })

    assert preview == {
        "title": "Article",
        "summary": "Summary",
        "tags": ["one", "two", "three"],
        "cover_url": "https://cdn.example.com/cover.jpg",
        "requested_platforms": ["blog", "wechat"],
    }


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("approved", "publish"),
        ("modified", "readapt"),
        ("rejected", "stop"),
        ("pending", "stop"),
    ],
)
def test_approval_router(status, expected):
    assert route_after_approval({"approval_status": status}) == expected


def _mock_valid_pipeline(monkeypatch, publish_calls):
    monkeypatch.setattr(graph_module, "ingest_node", lambda state: {
        "raw_content": "# Article\n\nBody",
        "images": [],
    })
    monkeypatch.setattr(graph_module, "format_optimize_node", lambda state: {
        "formatted_content": state["raw_content"],
    })
    monkeypatch.setattr(graph_module, "summary_meta_node", lambda state: {
        "title": "Article",
        "summary": "A valid summary.",
        "tags": ["one", "two", "three"],
    })
    monkeypatch.setattr(graph_module, "image_process_node", lambda state: {
        "content_with_oss_images": state["formatted_content"],
        "image_mapping": {},
        "oss_image_count": 0,
    })
    monkeypatch.setattr(graph_module, "cover_image_node", lambda state: {
        "cover_url": state.get("cover_url") or "https://cdn.example.com/cover.jpg",
    })

    def adapt(state):
        document = f"""---
title: {state['title']}
date: '2026-08-12'
tags: {state['tags']}
categories: [Technology]
layout: post
cover: {state['cover_url']}
description: {state['summary']}
---

# Article

Body"""
        return {"hexo_document": document, "wechat_draft": document}

    def publish(state):
        publish_calls.append(state["title"])
        return {"publish_results": [{"platform": "blog", "success": True}]}

    monkeypatch.setattr(graph_module, "content_adapt_node", adapt)
    monkeypatch.setattr(graph_module, "publish_node", publish)


def test_graph_pauses_before_publish_and_approve_resumes(monkeypatch):
    publish_calls = []
    _mock_valid_pipeline(monkeypatch, publish_calls)
    pipeline = PipelineGraph(Path(":memory:"))

    paused = pipeline.run({"requested_platforms": ["blog"]}, run_id="approve-run")
    assert paused["approval_status"] == "pending"
    assert paused["approval_request"]["preview"]["title"] == "Article"
    assert publish_calls == []

    completed = pipeline.resume("approve-run", {"action": "approve"})
    assert completed["approval_status"] == "approved"
    assert publish_calls == ["Article"]


def test_reject_stops_without_publish(monkeypatch):
    publish_calls = []
    _mock_valid_pipeline(monkeypatch, publish_calls)
    pipeline = PipelineGraph(Path(":memory:"))
    pipeline.run({"requested_platforms": ["blog"]}, run_id="reject-run")

    completed = pipeline.resume("reject-run", {"action": "reject"})
    assert completed["approval_status"] == "rejected"
    assert publish_calls == []


def test_modify_readapts_rechecks_and_pauses_again(monkeypatch):
    publish_calls = []
    _mock_valid_pipeline(monkeypatch, publish_calls)
    pipeline = PipelineGraph(Path(":memory:"))
    pipeline.run({"requested_platforms": ["blog"]}, run_id="modify-run")

    paused_again = pipeline.resume("modify-run", {
        "action": "modify",
        "changes": {
            "title": "Edited article",
            "summary": "Edited summary.",
            "tags": ["edited", "workflow", "review"],
        },
    })

    assert paused_again["approval_status"] == "pending"
    assert paused_again["approval_request"]["preview"]["title"] == "Edited article"
    assert paused_again["quality_check_count"] == 2
    assert publish_calls == []

    completed = pipeline.resume("modify-run", {"action": "approve"})
    assert completed["approval_status"] == "approved"
    assert publish_calls == ["Edited article"]
