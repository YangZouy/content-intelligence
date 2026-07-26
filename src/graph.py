"""
图编排 —— LangGraph StateGraph 流水线定义。

定义完整的 7 步线性流水线：
    ingest → format_optimize → summary_meta → image_process
    → cover_image → content_adapt → publish

提供 `run()` 入口函数，包装执行过程并附带可观测性
（追踪生成、节点耗时、运行日志持久化）。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from langgraph.graph import StateGraph, END

from src.state import AgentState
from src.nodes.ingest import ingest_node
from src.nodes.format_optimize import format_optimize_node
from src.nodes.summary_meta import summary_meta_node
from src.nodes.image_process import image_process_node
from src.nodes.cover import cover_image_node
from src.nodes.content_adapt import content_adapt_node
from src.nodes.publish import publish_node
from src.config_loader import get_config
from src.observability import TraceLogger, get_trace_logger


class PipelineGraph:
    """
    构建并管理LangGraph StateGraph流水线。
    """

    def __init__(self) -> None:
        # 编译图，需要单例缓存
        self._graph = self._build_graph()
        self._trace_logger = get_trace_logger()

    def _build_graph(self) -> Any:
        # Create graph with AgentState schema
        graph = StateGraph(AgentState)

        # Add all 7 nodes
        graph.add_node("ingest", ingest_node)
        graph.add_node("format_optimize", format_optimize_node)
        graph.add_node("summary_meta", summary_meta_node)
        graph.add_node("image_process", image_process_node)
        graph.add_node("cover_image", cover_image_node)
        graph.add_node("content_adapt", content_adapt_node)
        graph.add_node("publish", publish_node)

        # Define edges (linear pipeline)
        graph.add_edge("ingest", "format_optimize")
        graph.add_edge("format_optimize", "summary_meta")
        graph.add_edge("summary_meta", "image_process")
        graph.add_edge("image_process", "cover_image")
        graph.add_edge("cover_image", "content_adapt")
        graph.add_edge("content_adapt", "publish")
        graph.add_edge("publish", END)

        # Set entry point
        graph.set_entry_point("ingest")

        # 图编译
        return graph.compile()

    def run(
        self,
        input_state: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        """
        from loguru import logger

        trace_logger = get_trace_logger(force_new=True)
        # cid + time stamp + 密码学rand
        trace_id = trace_logger.generate_trace_id()
        run_start_time = time.time()

        # 将运行日志也记录到state中
        input_state["run_log"] = {
            "trace_id": trace_id,
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
            final_state = self._graph.invoke(input_state)

            # 记录用时
            total_duration = round(time.time() - run_start_time, 2)
            final_state.setdefault("run_log", {})["total_duration_seconds"] = total_duration

            # 节点用时
            node_durations = trace_logger.get_node_durations()
            final_state["run_log"]["node_durations"] = node_durations

            # token消耗
            if "token_usage_info" in final_state:
                final_state["run_log"]["token_usage"] = final_state.pop("token_usage_info")

            # 将run_log持久化到runs/...json文件中
            log_path = trace_logger.write_run_log(dict(final_state.get("run_log", {})))

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

# 模块级实例 初始化为None
_pipeline_instance: Optional[PipelineGraph] = None

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
    }

    # 拿到全局图实例
    pipeline = get_pipeline()
    # 返回图
    return pipeline.run(input_state)
