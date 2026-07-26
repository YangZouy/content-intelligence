"""
日志配置、追踪、运行日志落盘
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# python的第三方日志库
from loguru import logger

def _setup_logger() -> None:
    # 删除默认handler
    logger.remove()
    # 给所有日志加默认字段
    logger.configure(extra={"trace_id": ""})

    # 人看
    logger.add(
        sink=lambda msg: print(msg, end=""),
        level="INFO",
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level:<7}</level> | "
            "<cyan>{extra[trace_id]}</cyan> | "
            "{message}"
        ),
        colorize=True,
    )

    # 排查
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    logger.add(
        sink=log_dir / "dispatcher_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        format=(
            "{time:YYYY-MM-DDTHH:mm:ss.SSSZ} | "
            "{level:<7} | "
            "{extra[trace_id]} | "
            "{name}:{function}:{line} | {message}"
        ),
        # 单文件到5MB自动切新文件，避免一个日志无限膨胀
        rotation="5 MB",
        # 自动删7天前的日志
        retention="7 days",
        encoding="utf-8",
    )


# import时执行 与懒加载相反，日志需要在任何模块打第一条日志之前就配置好
_setup_logger()

# 数据类
@dataclass
class NodeTiming:
    """记录单个节点执行时间信息"""
    node_name: str
    start_time: float = 0.0
    end_time: float = 0.0

    @property
    def duration(self) -> float:
        """Duration in seconds."""
        if self.start_time == 0.0:
            return 0.0
        end = self.end_time or time.time()
        return round(end - self.start_time, 3)


class TraceLogger:
    def __init__(self) -> None:
        self._node_timings: Dict[str, NodeTiming] = {}

    @staticmethod
    def generate_trace_id() -> str:
        """
        uniq trace_id生成：
        'cid_' + 12-char hex timestamp + 8-char random hex.
        """
        ts = int(time.time() * 1000)
        import secrets
        # 使用密码学安全随机生成trace_id
        rand = secrets.token_hex(4)
        return f"cid_{ts:x}_{rand}"

    def node_enter(self, node_name: str, trace_id: str) -> None:
        timing = NodeTiming(node_name=node_name, start_time=time.time())
        self._node_timings[node_name] = timing
        # 用bind出来的logger打的每条日志输出时都会自动带上trace_id
        logger.bind(trace_id=trace_id).info(f">>> [{node_name}] ENTER")

    def node_exit(self, node_name: str, trace_id: str) -> None:
        timing = self._node_timings.get(node_name)
        if timing is not None:
            timing.end_time = time.time()
            logger.bind(trace_id=trace_id).info(
                f"<<< [{node_name}] EXIT ({timing.duration:.2f}s)"
            )
        else:
            logger.bind(trace_id=trace_id).warning(
                f"<<< [{node_name}] EXIT (no enter recorded)"
            )

    def get_node_durations(self) -> Dict[str, float]:
        return {
            name: timing.duration
            for name, timing in self._node_timings.items()
        }

    def write_run_log(self, run_log: Dict[str, Any]) -> Path:
        """
        写结构化运行日志/审计trail，可用于事后排查，成本统计，计算性能基线
        """
        runs_dir = Path("runs")
        runs_dir.mkdir(parents=True, exist_ok=True)

        # Generate filename from timestamp
        timestamp = run_log.get(
            "timestamp",
            datetime.now(timezone.utc).isoformat(),
        )
        safe_ts = timestamp.replace(":", "-").replace(".", "-")[:19]
        filepath = runs_dir / f"{safe_ts}.json"

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(run_log, f, ensure_ascii=False, indent=2)

        logger.bind(trace_id=run_log.get("trace_id", "")).info(
            f"Run log written to {filepath}"
        )

        return filepath


# Module-level singleton
_trace_logger_instance: Optional[TraceLogger] = None


def get_trace_logger(force_new: bool = False) -> TraceLogger:
    global _trace_logger_instance
    if _trace_logger_instance is None or force_new:
        _trace_logger_instance = TraceLogger()
    return _trace_logger_instance
