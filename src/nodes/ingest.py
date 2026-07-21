"""
摄取节点 - 多源内容导入。

处理两种输入类型：
1. Obsidian: 本地 .md 文件 + 相邻的 assets/ 目录
   标记中的图像以 ![](name.png) 形式引用，并存储在
   同级的 assets/ 文件夹中。
2. 标记链接：本地 .md 文件，带有远程（https://）图像URL。

输出：{source_type, raw_content, images: ImageRef[], file_path}
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

from src.errors import IngestError
from src.state import AgentState, ImageRef
from src.observability import get_trace_logger


# ---------------------------------------------------------------------------
# Image extraction patterns
# ---------------------------------------------------------------------------

# Markdown inline image: ![alt](url)
_MD_IMAGE_PATTERN = re.compile(
    r"!\[([^\]]*)\]\(([^)]+)\)"
)

# HTML image tag: <img src="url" alt="text" ...>
_HTML_IMAGE_PATTERN = re.compile(
    r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>'
)


def _resolve_obsidian_image(ref: str, md_path: Path) -> str:
    """将Obsidian的图像引用解析为真实的本地文件路径。

    Obsidian将笔记附件存储在同级的 'assets/' 文件夹中。

    - 远程引用（http://、https://、data:）按原样返回。
    - 存在且绝对的本地路径按原样返回。

    参数：
        ref: 来自标记（![alt](ref)）的原始图像引用。
        md_path: 源标记文件的路径（用于同级解析）。

    返回：
        可用的URL或本地文件系统路径。
    """
    if ref.startswith(("http://", "https://", "data:")):
        return ref  # remote / inline -> keep

    ref_path = Path(ref)
    if ref_path.is_absolute():
        return str(ref_path) if ref_path.exists() else ref

    parent = md_path.parent
    candidates = [
        parent / "assets" / ref,           # Obsidian default: assets/<name>
    ]
    for cand in candidates:
        if cand.exists():
            return str(cand.resolve())
    # Could not resolve locally -> keep original so the failure is loud downstream
    return ref


def _extract_images_from_content(content: str, md_path: Path) -> List[ImageRef]:
    """从markdown/HTML内容中提取所有图像引用。

    支持两类输入（与上游约定一致）：
    1. Obsidian 笔记：图像以 ``![](name.png)`` 形式引用，真实文件保存在
       笔记同级的 ``assets/`` 目录中。
       -> 解析为真实本地文件路径后，由下游上传到 OSS。
    2. 带外链的 markdown：图像以 ``https://...`` 形式引用，不在本地。
       -> 原样保留远程 URL，由下游重新下载并上传到 OSS。

    对于 Obsidian 风格引用，只有当能在本地解析到一个真实存在的文件时才
    保留该引用；无法解析的裸文件名（如文件缺失）会被跳过，避免把裸文件名
    当作本地路径传给 OSS 上传而失败。

    参数：
        content: 要扫描的原始文本内容。
        md_path: 源markdown文件的路径（用于本地解析）。

    返回：
        ImageRef字典列表，包含 url_or_path、alt、usage='inline'。
    """
    images: List[ImageRef] = []
    seen: set = set()

    def _add(ref: str, alt: str) -> None:
        if not ref:
            return

        # 远程图像（http/https/data URI）：原样保留 URL
        if ref.startswith(("http://", "https://", "data:")):
            if ref not in seen:
                seen.add(ref)
                images.append(ImageRef(
                    url_or_path=ref,
                    alt=alt or "",
                    usage="inline",
                ))
            return

        # 本地 Obsidian 图片引用：解析为真实文件路径
        resolved = _resolve_obsidian_image(ref, md_path)
        # 仅当解析结果指向一个真实存在的本地文件时才保留；
        # 否则跳过（裸文件名无法上传）。
        if resolved and Path(resolved).exists():
            if resolved not in seen:
                seen.add(resolved)
                images.append(ImageRef(
                    url_or_path=resolved,
                    alt=alt or "",
                    usage="inline",
                ))

    # 提取![]()格式图片
    for match in _MD_IMAGE_PATTERN.finditer(content):
        _add(match.group(2).strip(), match.group(1).strip())

    # 提取<img src=>格式图片
    for match in _HTML_IMAGE_PATTERN.finditer(content):
        _add(match.group(1).strip(), "")

    return images


def _read_local_file(file_path: str) -> str:
    """Read a local file's text content with UTF-8 encoding.

    Args:
        file_path: Absolute or relative path to file.

    Returns:
        File contents as string.

    Raises:
        IngestError: If file cannot be read.
    """
    path = Path(file_path)
    if not path.exists():
        raise IngestError(f"File not found: {file_path}")

    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise IngestError(f"Failed to read file '{file_path}': {e}") from e


def _detect_source_type(file_path: str) -> str:
    """Detect the source type of an input file based on path/context.

    Currently defaults to 'markdown_link' since all local .md files are
    treated uniformly. Future extension could detect Obsidian vault structure.

    Args:
        file_path: Path to the input file.

        Returns:
            Source type string: 'obsidian' or 'markdown_link'.
    """
    path = Path(file_path)

    # Local markdown note (Obsidian vault) by default
    return "obsidian"


# ---------------------------------------------------------------------------
# Main node function
# ---------------------------------------------------------------------------

def ingest_node(state: AgentState) -> Dict[str, Any]:
    """从指定来源摄取内容到原始流水线状态。

    这是流水线中的第一个节点。它读取输入文件，
    提取所有图像引用，确定来源类型，并
    填充InputState字段。

    节点签名遵循C1约定：(state: AgentState) -> dict
    仅返回部分更新字典（C2规则）。

    参数：
        state: 当前代理状态。必须至少包含 `file_path` 键。

    返回：
        部分状态更新，包含：
        - source_type: 检测到的来源类型字符串。
        - raw_content: 来源的完整文本内容。
        - images: 在内容中或内容附近找到的ImageRef列表。
        - file_path: 确认的文件路径。
    """
    trace_logger = get_trace_logger()
    trace_id = state.get("run_log", {}).get("trace_id", "")
    trace_logger.node_enter("ingest", trace_id)

    try:
        file_path = state.get("file_path", "")
        if not file_path:
            raise IngestError("No file_path provided in state for ingestion")

        source_type = state.get("source_type") or _detect_source_type(file_path)
        # 读取本地文件内容
        raw_content = _read_local_file(file_path)

        # 图片处理：从正文提取 ![](...) 并解析为真实路径
        all_images = _extract_images_from_content(raw_content, Path(file_path))

        # Build partial state update
        result = {
            "source_type": source_type,
            "raw_content": raw_content,
            "images": all_images,
            "file_path": file_path,
        }

        logger_instance = __import__("loguru").logger
        logger_instance.bind(trace_id=trace_id).info(
            f"Ingested '{Path(file_path).name}' "
            f"(type={source_type}, images={len(all_images)}, "
            f"chars={len(raw_content)})"
        )

        return result

    except IngestError:
        raise  # Re-raise known errors as-is
    except Exception as e:
        log = __import__("loguru").logger
        log.bind(trace_id=trace_id).error(f"Ingest failed: {e}")
        raise IngestError(f"Content ingestion failed: {e}") from e
    finally:
        trace_logger.node_exit("ingest", trace_id)
