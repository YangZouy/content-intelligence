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
    """构建并管理LangGraph StateGraph流水线。
    
    该流水线是一个线性6节点有向无环图，每个节点处理状态
    并将部分更新传递给下一个节点。

    属性:
        _graph: 已编译的、可供调用的StateGraph。
        _trace_logger: 每次运行的跟踪日志记录器实例。
    """

    def __init__(self) -> None:
        """Build and compile the StateGraph pipeline."""
        self._graph = self._build_graph()
        self._trace_logger = get_trace_logger()

    def _build_graph(self) -> Any:
        """构建包含6个节点和线性边的StateGraph。

        图拓扑结构（线性流水线）：
            [ingest] → [format_optimize] → [summary_meta] →
            [image_process] → [cover_image] → [content_adapt] → [publish] → END

        返回：
            已编译的LangGraph StateGraph。
        """
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

        # Compile for execution
        return graph.compile()

    def run(
        self,
        input_state: Dict[str, Any],
        preview_mode: bool = False,
    ) -> Dict[str, Any]:
        """以可观测性方式执行完整流水线。

        这是供CLI或编程式调用者使用的主入口点。
        封装图调用，包含：
        - 跟踪ID生成
        - 节点进入/退出计时
        - 运行日志持久化到 runs/<timestamp>.json
        - 总耗时追踪

        参数：
            input_state: 初始状态字典，至少包含
                'file_path'（内容文件路径），可选包含
                'requested_platforms'、'source_type'、'brand'。
            preview_mode: 如果为True，则在content_adapt节点之后停止
                （跳过发布）。供 `preview` CLI命令使用。

        返回：
            完整（或部分）流水线执行后的最终代理状态字典。

        异常：
            CIDError: 如果任何节点引发不可恢复的错误。
        """
        from loguru import logger

        # Initialize trace logger for this run
        trace_logger = get_trace_logger(force_new=True)
        trace_id = trace_logger.generate_trace_id()
        run_start_time = time.time()

        # Initialize run log entry in state
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
            f"| preview={preview_mode} "
            f"| platforms={input_state.get('requested_platforms', [])}"
        )

        try:
            # Execute the compiled graph
            final_state = self._graph.invoke(input_state)

            # Record total duration
            total_duration = round(time.time() - run_start_time, 2)
            final_state.setdefault("run_log", {})["total_duration_seconds"] = total_duration

            # Collect node durations from trace logger
            node_durations = trace_logger.get_node_durations()
            final_state["run_log"]["node_durations"] = node_durations

            # Collect token usage (if available from LLM call)
            if "token_usage_info" in final_state:
                final_state["run_log"]["token_usage"] = final_state.pop("token_usage_info")

            # Persist run log
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


# Module-level singleton
_pipeline_instance: Optional[PipelineGraph] = None

def get_pipeline() -> PipelineGraph:
    """获取单例PipelineGraph实例。

    返回：
        共享的PipelineGraph实例。
    """
    global _pipeline_instance
    if _pipeline_instance is None:
        _pipeline_instance = PipelineGraph()
    return _pipeline_instance


def run_pipeline(
    file_path: str,
    platforms: Optional[list[str]] = None,
    preview_mode: bool = False,
    format_optimize_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """
    运行完整流水线的便捷函数。

    这是 CLI 和外部调用者使用的主要 API 入口。

    参数：
        file_path: Markdown 内容文件路径。
        platforms: 平台键名列表（'blog', 'wechat'）。
            默认使用 config 中 default_options.platforms 的值。
        preview_mode: 若为 True，则跳过发布步骤。
        format_optimize_mode: 覆盖 config.format_optimize.mode
            （'rule' | 'llm'）；为 None 时沿用配置。

    返回：
        最终流水线状态字典。
    """
    config = get_config()

    # 命令行覆盖 format_optimize 模式（运行时改单例，不影响磁盘配置）
    if format_optimize_mode in ("rule", "llm"):
        config.format_optimize["mode"] = format_optimize_mode

    input_state: Dict[str, Any] = {
        "file_path": file_path,
        "requested_platforms": platforms or list(config.get_platforms()),
        "brand": config.get_brand_config(),
    }

    pipeline = get_pipeline()
    return pipeline.run(input_state, preview_mode=preview_mode)
