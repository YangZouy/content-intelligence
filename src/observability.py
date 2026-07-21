"""
Observability Module - Logging, tracing, and run log persistence.

Provides:
- Loguru-based structured logging with trace_id correlation
- TraceLogger for per-run lifecycle management
- Automatic run log JSON persistence to runs/<timestamp>.json

Usage:
    from src.observability import get_trace_logger
    logger = get_trace_logger()
    trace_id = logger.generate_trace_id()
    logger.node_enter("ingest", trace_id)
    # ... do work ...
    logger.node_exit("ingest", trace_id)
    logger.write_run_log(run_data)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from loguru import logger


# --- Configure Loguru ---

def _setup_logger() -> None:
    """Configure loguru with console and file handlers.

    Console: INFO level with color.
    File: DEBUG level with rotation (5MB) in logs/ directory.
    """
    # Remove default handler
    logger.remove()

    # Set a default `trace_id` extra so the format string {extra[trace_id]}
    # never raises KeyError when a module logs without binding trace_id
    # (e.g. github_pages.py uses the bare root logger for error logs).
    logger.configure(extra={"trace_id": ""})

    # Console handler - human-readable
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

    # File handler - structured debug logs
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
        rotation="5 MB",
        retention="7 days",
        encoding="utf-8",
    )


# Run setup on import
_setup_logger()


@dataclass
class NodeTiming:
    """Records timing information for a single node execution."""

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
    """Manages per-run trace logging and run log persistence.

    Each pipeline execution creates a unique trace_id that is included in
    all log messages for that run, enabling easy log correlation.

    Attributes:
        _node_timings: Dict tracking start/end times for each node.
    """

    def __init__(self) -> None:
        self._node_timings: Dict[str, NodeTiming] = {}

    @staticmethod
    def generate_trace_id() -> str:
        """Generate a unique trace ID for a pipeline run.

        Format: 'cid_' + 12-char hex timestamp + 8-char random hex.

        Returns:
            Unique trace identifier string.
        """
        ts = int(time.time() * 1000)
        import secrets
        rand = secrets.token_hex(4)
        return f"cid_{ts:x}_{rand}"

    def node_enter(self, node_name: str, trace_id: str) -> None:
        """Record the entry time of a node.

        Also emits an INFO-level log message.

        Args:
            node_name: Name of the node being entered.
            trace_id: Current run's trace ID.
        """
        timing = NodeTiming(node_name=node_name, start_time=time.time())
        self._node_timings[node_name] = timing
        logger.bind(trace_id=trace_id).info(f">>> [{node_name}] ENTER")

    def node_exit(self, node_name: str, trace_id: str) -> None:
        """Record the exit time of a node and log duration.

        Args:
            node_name: Name of the node being exited.
            trace_id: Current run's trace ID.
        """
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
        """Get all recorded node durations.

        Returns:
            Dict mapping node name to duration in seconds.
        """
        return {
            name: timing.duration
            for name, timing in self._node_timings.items()
        }

    def write_run_log(self, run_log: Dict[str, Any]) -> Path:
        """Persist the run log as a JSON file in runs/ directory.

        Args:
            run_log: Complete run log dictionary to serialize.

        Returns:
            Path to the written JSON file.
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
    """Get the global TraceLogger instance.

    Args:
        force_new: If True, create fresh instance.

    Returns:
        Shared TraceLogger instance.
    """
    global _trace_logger_instance
    if _trace_logger_instance is None or force_new:
        _trace_logger_instance = TraceLogger()
    return _trace_logger_instance
