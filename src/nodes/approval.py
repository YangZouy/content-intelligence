"""Human approval gate before side-effecting publish operations."""

from __future__ import annotations

from typing import Any, Dict, Literal, Mapping

from langgraph.types import interrupt

from src.observability import get_trace_logger
from src.state import AgentState


ApprovalRoute = Literal["publish", "readapt", "stop"]
_EDITABLE_FIELDS = {"title", "summary", "tags", "cover_url", "requested_platforms"}


def build_approval_preview(state: AgentState) -> Dict[str, Any]:
    """Build the small, serializable payload shown to the reviewer."""
    return {
        "title": state.get("title", ""),
        "summary": state.get("summary", ""),
        "tags": state.get("tags", []),
        "cover_url": state.get("cover_url", ""),
        "requested_platforms": state.get("requested_platforms", []),
    }


def _normalize_decision(response: Any) -> Dict[str, Any]:
    if isinstance(response, str):
        return {"action": response}
    if isinstance(response, Mapping):
        return dict(response)
    raise ValueError("approval response must be a string or mapping")


def approval_node(state: AgentState) -> Dict[str, Any]:
    """Pause the graph and apply the reviewer's decision after resume."""
    trace_logger = get_trace_logger()
    trace_id = state.get("run_log", {}).get("trace_id", "")
    trace_logger.node_enter("approval", trace_id)
    try:
        response = _normalize_decision(interrupt({
            "kind": "publish_approval",
            "message": "Review content before publishing",
            "preview": build_approval_preview(state),
            "allowed_actions": ["approve", "reject", "modify"],
        }))
        action = str(response.get("action", "")).lower()
        note = str(response.get("note", ""))

        if action == "approve":
            return {
                "approval_status": "approved",
                "approval_decision": action,
                "approval_note": note,
            }
        if action == "reject":
            return {
                "approval_status": "rejected",
                "approval_decision": action,
                "approval_note": note,
            }
        if action == "modify":
            changes = response.get("changes")
            if not isinstance(changes, Mapping) or not changes:
                raise ValueError("modify decision requires a non-empty 'changes' mapping")
            unknown = set(changes) - _EDITABLE_FIELDS
            if unknown:
                raise ValueError(f"unsupported approval fields: {sorted(unknown)}")
            update = {key: value for key, value in changes.items() if key in _EDITABLE_FIELDS}
            update.update({
                "approval_status": "modified",
                "approval_decision": action,
                "approval_note": note,
                "quality_status": "pending",
            })
            return update
        raise ValueError("approval action must be approve, reject, or modify")
    finally:
        trace_logger.node_exit("approval", trace_id)


def route_after_approval(state: AgentState) -> ApprovalRoute:
    status = state.get("approval_status")
    if status == "approved":
        return "publish"
    if status == "modified":
        return "readapt"
    return "stop"
