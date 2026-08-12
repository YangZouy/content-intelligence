"""Normalize and aggregate token usage from LangChain model responses."""

from __future__ import annotations

from typing import Any, Dict, Mapping


TOKEN_KEYS = ("input_tokens", "output_tokens", "total_tokens")


def extract_token_usage(message: Any) -> Dict[str, int]:
    """Read provider-neutral usage metadata from an AIMessage-like object."""
    usage = getattr(message, "usage_metadata", None)
    if not isinstance(usage, Mapping):
        metadata = getattr(message, "response_metadata", {}) or {}
        usage = metadata.get("token_usage") or metadata.get("usage") or {}

    input_tokens = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
    output_tokens = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
    total_tokens = int(usage.get("total_tokens", input_tokens + output_tokens) or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def merge_token_usage(
    current: Mapping[str, int] | None,
    additional: Mapping[str, int] | None,
) -> Dict[str, int]:
    """Add one model call's usage to the workflow total."""
    current = current or {}
    additional = additional or {}
    return {
        key: int(current.get(key, 0)) + int(additional.get(key, 0))
        for key in TOKEN_KEYS
    }
