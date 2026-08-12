"""
图编排 —— LangGraph StateGraph 流水线定义。

定义带质量门禁和一次有限修复的确定性工作流：
    ingest → format_optimize → summary_meta → image_process
    → cover_image → content_adapt → quality_check
    → passed: publish
    → failed: quality_repair → summary_meta（最多一次）

提供 `run()` 入口函数，包装执行过程并附带可观测性
（追踪生成、节点耗时、运行日志持久化）。
"""

from __future__ import annotations

import time
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import StateGraph, END
from langgraph.types import Command

from src.state import AgentState
from src.nodes.ingest import ingest_node
from src.nodes.article_identity import article_identity_node
from src.nodes.format_optimize import format_optimize_node
from src.nodes.summary_meta import summary_meta_node
from src.nodes.image_process import image_process_node
from src.nodes.cover import cover_image_node
from src.nodes.content_adapt import content_adapt_node
from src.nodes.publish import publish_blog_node, publish_wechat_node
from src.nodes.quality_check import quality_check_node
from src.nodes.quality_repair import quality_repair_node
from src.nodes.approval import approval_node, route_after_approval
from src.config_loader import get_config
from src.observability import TraceLogger, get_trace_logger


class PipelineGraph:
    """
    构建并管理LangGraph StateGraph流水线。
    """

    def __init__(self, checkpoint_path: Optional[Path] = None) -> None:
        checkpoint_file = checkpoint_path or Path("checkpoints/workflow.sqlite")
        checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
        self._checkpoint_connection = sqlite3.connect(
            checkpoint_file,
            check_same_thread=False,
        )
        self._checkpointer = SqliteSaver(self._checkpoint_connection)
        self._graph = self._build_graph()
        self._trace_logger = get_trace_logger()

    def _build_graph(self) -> Any:
        # Create graph with AgentState schema
        graph = StateGraph(AgentState)

        graph.add_node("ingest", ingest_node)
        graph.add_node("article_identity", article_identity_node)
        graph.add_node("format_optimize", format_optimize_node)
        graph.add_node("summary_meta", summary_meta_node)
        graph.add_node("image_process", image_process_node)
        graph.add_node("cover_image", cover_image_node)
        graph.add_node("content_adapt", content_adapt_node)
        graph.add_node("quality_check", quality_check_node)
        graph.add_node("quality_repair", quality_repair_node)
        graph.add_node("approval", approval_node)
        graph.add_node("publish_blog", publish_blog_node)
        graph.add_node("publish_wechat", publish_wechat_node)

        graph.add_edge("ingest", "article_identity")
        graph.add_edge("article_identity", "format_optimize")
        graph.add_edge("format_optimize", "summary_meta")
        graph.add_edge("summary_meta", "image_process")
        graph.add_edge("image_process", "cover_image")
        graph.add_edge("cover_image", "content_adapt")
        graph.add_edge("content_adapt", "quality_check")
        graph.add_conditional_edges(
            "quality_check",
            route_after_quality_check,
            {
                "publish": "approval",
                "repair": "quality_repair",
                "stop": END,
            },
        )
        graph.add_edge("quality_repair", "summary_meta")
        graph.add_conditional_edges(
            "approval",
            route_after_approval,
            {
                "publish": "publish_blog",
                "readapt": "content_adapt",
                "stop": END,
            },
        )
        graph.add_edge("publish_blog", "publish_wechat")
        graph.add_edge("publish_wechat", END)

        # Set entry point
        graph.set_entry_point("ingest")

        # 图编译
        return graph.compile(checkpointer=self._checkpointer)

    def run(
        self,
        input_state: Dict[str, Any],
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        """
        from loguru import logger

        trace_logger = get_trace_logger(force_new=True)
        # cid + time stamp + 密码学rand
        trace_id = trace_logger.generate_trace_id()
        resolved_run_id = run_id or uuid.uuid4().hex
        run_start_time = time.time()

        # 将运行日志也记录到state中
        input_state["run_log"] = {
            "trace_id": trace_id,
            "run_id": resolved_run_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source_type": input_state.get("source_type", "unknown"),
            "file_name": "",
            "platforms_requested": input_state.get(
                "requested_platforms", []
            ),
            "node_durations": {},
            "token_usage": {},
            "publish_results": [],
            "total_duration_seconds": 0,
            "oss_uploads": [],
        }

        # Extract file name for logging
        file_path = input_state.get("file_path", "")
        if file_path:
            from pathlib import Path
            input_state["run_log"]["file_name"] = Path(file_path).name

        logger.bind(trace_id=trace_id).info(
            f"Pipeline started | file={input_state['run_log']['file_name']} "
            f"| platforms={input_state.get('requested_platforms', [])}"
        )

        try:
            config = {"configurable": {"thread_id": resolved_run_id}}
            result = self._graph.invoke(input_state, config=config)
            final_state = self._state_with_interrupt(result, config, resolved_run_id)

            if final_state.get("approval_status") == "pending":
                return final_state

            # 记录用时
            total_duration = round(time.time() - run_start_time, 2)
            final_state.setdefault("run_log", {})["total_duration_seconds"] = total_duration

            log_path = self._finalize_run(final_state)

            if final_state.get("quality_status") == "failed":
                logger.bind(trace_id=trace_id).warning(
                    f"Pipeline stopped by quality gate after {total_duration}s | "
                    f"log={log_path.name}"
                )
            else:
                logger.bind(trace_id=trace_id).success(
                    f"Pipeline completed successfully in {total_duration}s | "
                    f"log={log_path.name}"
                )

            return final_state

        except Exception as e:
            total_duration = round(time.time() - run_start_time, 2)
            input_state["run_log"]["total_duration_seconds"] = total_duration
            input_state["run_log"].setdefault("publish_results", [])

            logger.bind(trace_id=trace_id).error(
                f"Pipeline FAILED after {total_duration}s: {e}"
            )

            # Still write run log even on failure
            try:
                trace_logger.write_run_log(dict(input_state["run_log"]))
            except Exception:
                pass  # Don't mask original error with write failure

            raise

    def resume(self, run_id: str, decision: Dict[str, Any]) -> Dict[str, Any]:
        """Resume a checkpointed approval using its stable run identifier."""
        config = {"configurable": {"thread_id": run_id}}
        snapshot = self._graph.get_state(config)
        if not snapshot.values:
            raise ValueError(f"No checkpoint found for run_id={run_id}")
        if not snapshot.interrupts:
            raise ValueError(f"Run {run_id} is not waiting for approval")

        result = self._graph.invoke(Command(resume=decision), config=config)
        final_state = self._state_with_interrupt(result, config, run_id)
        if final_state.get("approval_status") != "pending":
            self._finalize_run(final_state)
        return final_state

    def pending_approval(self, run_id: str) -> Dict[str, Any]:
        """Read a paused approval without advancing the graph."""
        config = {"configurable": {"thread_id": run_id}}
        snapshot = self._graph.get_state(config)
        if not snapshot.values:
            raise ValueError(f"No checkpoint found for run_id={run_id}")
        if not snapshot.interrupts:
            raise ValueError(f"Run {run_id} is not waiting for approval")
        state = dict(snapshot.values)
        state["approval_status"] = "pending"
        state["approval_request"] = snapshot.interrupts[0].value
        state["run_id"] = run_id
        return state

    def retry_publish(self, run_id: str) -> Dict[str, Any]:
        """Retry failed platforms from a completed checkpointed run."""
        config = {"configurable": {"thread_id": run_id}}
        snapshot = self._graph.get_state(config)
        if not snapshot.values:
            raise ValueError(f"No checkpoint found for run_id={run_id}")
        if snapshot.interrupts:
            raise ValueError(f"Run {run_id} is still waiting for approval")
        state = dict(snapshot.values)
        if state.get("approval_status") != "approved":
            raise ValueError(f"Run {run_id} was not approved for publishing")
        if all(
            item.get("success")
            for item in state.get("publish_results", [])
            if item.get("platform") in state.get("requested_platforms", [])
        ) and len(state.get("publish_results", [])) >= len(state.get("requested_platforms", [])):
            return state

        self._graph.update_state(config, {}, as_node="approval")
        result = self._graph.invoke(None, config=config)
        final_state = self._state_with_interrupt(result, config, run_id)
        self._finalize_run(final_state)
        return final_state

    def _state_with_interrupt(
        self,
        result: Dict[str, Any],
        config: Dict[str, Any],
        run_id: str,
    ) -> Dict[str, Any]:
        final_state = dict(result)
        snapshot = self._graph.get_state(config)
        if snapshot.interrupts:
            final_state["approval_status"] = "pending"
            final_state["approval_request"] = snapshot.interrupts[0].value
            final_state["run_id"] = run_id
        return final_state

    def _finalize_run(self, final_state: Dict[str, Any]) -> Path:
        run_log = final_state.setdefault("run_log", {})
        run_log["node_durations"] = get_trace_logger().get_node_durations()
        run_log["quality_gate"] = {
            "status": final_state.get("quality_status", "pending"),
            "check_count": final_state.get("quality_check_count", 0),
            "repair_count": final_state.get("quality_repair_count", 0),
            "issues": final_state.get("quality_issues", []),
        }
        run_log["approval"] = {
            "status": final_state.get("approval_status", "not_requested"),
            "decision": final_state.get("approval_decision", ""),
            "note": final_state.get("approval_note", ""),
        }
        run_log["publish_results"] = list(final_state.get("publish_results", []))
        if "token_usage_info" in final_state:
            run_log["token_usage"] = final_state.pop("token_usage_info")
        return get_trace_logger().write_run_log(dict(run_log))

# 模块级实例 初始化为None
_pipeline_instance: Optional[PipelineGraph] = None

# 判断下一步去哪儿，不负责执行修复或发布
def route_after_quality_check(
    state: AgentState,
) -> Literal["publish", "repair", "stop"]:
    """Route only passed content to side-effecting publishers."""
    status = state.get("quality_status")
    if status == "passed":
        return "publish"
    if status == "needs_repair" and state.get("quality_repair_count", 0) < 1:
        return "repair"
    return "stop"

def get_pipeline() -> PipelineGraph:
    # 全局实例
    global _pipeline_instance
    if _pipeline_instance is None:
        _pipeline_instance = PipelineGraph()
    return _pipeline_instance


def run_pipeline(
    file_path: str,
    platforms: Optional[list[str]] = None,
    format_optimize_mode: Optional[str] = None,
    article_slug: Optional[str] = None,
) -> Dict[str, Any]:
    """
    运行完整流水线的便捷函数。

    参数：
        file_path: 文件路径。
        platforms: blog/wechat
        format_optimize_mode: rule/llm
    """
    config = get_config()

    input_state: Dict[str, Any] = {
        "file_path": file_path,
        "requested_platforms": platforms or list(config.get_platforms()),
        "brand": config.get_brand_config(),
        "format_optimize_mode": format_optimize_mode,
        "article_slug": article_slug or "",
    }

    # 拿到全局图实例
    pipeline = get_pipeline()
    # 返回图
    return pipeline.run(input_state)


def resume_pipeline(run_id: str, decision: Dict[str, Any]) -> Dict[str, Any]:
    """Resume a workflow paused at its publish approval checkpoint."""
    return get_pipeline().resume(run_id, decision)


def get_pending_approval(run_id: str) -> Dict[str, Any]:
    """Return the preview for a workflow waiting at human approval."""
    return get_pipeline().pending_approval(run_id)


def retry_pipeline_publish(run_id: str) -> Dict[str, Any]:
    """Retry only failed platform publications from a checkpoint."""
    return get_pipeline().retry_publish(run_id)
