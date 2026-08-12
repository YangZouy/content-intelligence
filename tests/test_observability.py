from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.nodes.publish as publish_module
import src.nodes.summary_meta as summary_module
from src.errors import OSSError, retry_with_backoff
from src.observability import get_trace_logger
from src.schema import SummaryMetaOutput
from src.usage import extract_token_usage, merge_token_usage


def test_extracts_langchain_usage_metadata():
    message = SimpleNamespace(
        usage_metadata={
            "input_tokens": 120,
            "output_tokens": 30,
            "total_tokens": 150,
        },
        response_metadata={},
    )
    assert extract_token_usage(message) == {
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
    }


def test_extracts_openai_compatible_response_metadata():
    message = SimpleNamespace(
        usage_metadata=None,
        response_metadata={
            "token_usage": {
                "prompt_tokens": 80,
                "completion_tokens": 20,
                "total_tokens": 100,
            }
        },
    )
    assert extract_token_usage(message)["total_tokens"] == 100


def test_token_usage_accumulates_across_model_calls():
    assert merge_token_usage(
        {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        {"input_tokens": 60, "output_tokens": 10, "total_tokens": 70},
    ) == {
        "input_tokens": 160,
        "output_tokens": 30,
        "total_tokens": 190,
    }


def test_retry_decorator_records_structured_event(monkeypatch):
    trace = get_trace_logger(force_new=True)
    monkeypatch.setattr("src.errors.time.sleep", lambda delay: None)
    calls = 0

    @retry_with_backoff(max_attempts=2, base_delay=1)
    def flaky():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSSError("temporary OSS failure")
        return "ok"

    assert flaky() == "ok"
    assert trace.get_retry_events() == [{
        "operation": "test_retry_decorator_records_structured_event.<locals>.flaky",
        "attempt": 1,
        "max_attempts": 2,
        "error_type": "OSSError",
        "error": "temporary OSS failure",
        "delay_seconds": 1,
        "timestamp": trace.get_retry_events()[0]["timestamp"],
    }]


def test_publish_retry_records_failure_and_final_attempt(monkeypatch):
    trace = get_trace_logger(force_new=True)
    monkeypatch.setattr("time.sleep", lambda delay: None)

    class Publisher:
        platform = "wechat"

        def __init__(self):
            self.calls = 0

        def publish(self, content, brand):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary publish failure")
            return {
                "platform": "wechat",
                "success": True,
                "url": "media_id:ok",
                "attempt": 1,
                "error": None,
            }

    result = publish_module._run_single_publish(
        Publisher(),
        "content",
        {},
        "wechat:key",
        max_retries=1,
    )
    assert result["attempt"] == 2
    assert trace.get_retry_events()[0]["operation"] == "publish.wechat"
    assert trace.get_retry_events()[0]["delay_seconds"] == 2


def test_summary_node_writes_and_accumulates_real_response_usage(monkeypatch):
    raw = SimpleNamespace(
        usage_metadata={
            "input_tokens": 90,
            "output_tokens": 10,
            "total_tokens": 100,
        },
        response_metadata={},
    )

    class StructuredModel:
        def invoke(self, messages):
            return {
                "raw": raw,
                "parsed": SummaryMetaOutput(
                    title="Article",
                    summary="Summary",
                    tags=["one", "two", "three"],
                    word_count=100,
                ),
                "parsing_error": None,
            }

    class Model:
        def with_structured_output(self, schema, **kwargs):
            assert kwargs["include_raw"] is True
            return StructuredModel()

    monkeypatch.setattr(summary_module, "get_summary_model", lambda: Model())
    result = summary_module.summary_meta_node({
        "formatted_content": "# Article\n\nBody",
        "token_usage_info": {
            "input_tokens": 20,
            "output_tokens": 5,
            "total_tokens": 25,
        },
    })

    assert result["token_usage_info"] == {
        "input_tokens": 110,
        "output_tokens": 15,
        "total_tokens": 125,
    }
