"""Prepare one bounded regeneration attempt from quality-gate findings."""

from __future__ import annotations

from typing import Any, Dict

from src.observability import get_trace_logger
from src.state import AgentState


MAX_QUALITY_REPAIRS = 1


def quality_repair_node(state: AgentState) -> Dict[str, Any]:
    """Convert deterministic findings into feedback for one regeneration.

    The node does not mutate content itself. The graph returns to
    ``summary_meta`` so metadata, image processing and platform adaptation are
    regenerated through the normal, observable workflow.
    """
    trace_logger = get_trace_logger()
    trace_id = state.get("run_log", {}).get("trace_id", "")
    trace_logger.node_enter("quality_repair", trace_id)
    try:
        repair_count = state.get("quality_repair_count", 0)
        if repair_count >= MAX_QUALITY_REPAIRS:
            raise RuntimeError("quality repair limit exceeded")

        issues = state.get("quality_issues", [])
        feedback_lines = [
            f"- {item.get('code', 'quality.unknown')}: {item.get('message', '')}"
            for item in issues
        ]
        feedback = "\n".join(feedback_lines) or "- quality.unknown: quality check failed"

        return {
            "quality_repair_count": repair_count + 1,
            "quality_status": "pending",
            "quality_feedback": feedback,
        }
    finally:
        trace_logger.node_exit("quality_repair", trace_id)
