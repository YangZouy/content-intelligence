"""
Summary & Metadata Node - LLM-powered title/summary/tag extraction.

This is the **ONLY** LLM call point in the entire pipeline.
Uses gpt-4o-mini with structured output to extract metadata from formatted content.

Outputs: {title, summary, tags, word_count, reading_time}
"""

from __future__ import annotations

import math
from typing import Any, Dict

from langchain_core.messages import HumanMessage, SystemMessage

from src.errors import LLMError
from src.llm import get_summary_model
from src.schema import SummaryMetaOutput
from src.state import AgentState
from src.observability import get_trace_logger


# ---------------------------------------------------------------------------
# System prompt template
# ---------------------------------------------------------------------------

SUMMARY_SYSTEM_PROMPT = """你是一位专业的内容分析师，擅长从技术文章中提取结构化元数据。

你的任务是从给定的 Markdown 文章内容中提取以下信息：

1. **标题**：如果原文已有明确的 H1 标题，保持不变；否则根据内容提炼一个简洁的中文标题（≤30字）
2. **摘要**：用一句话概括文章核心观点和主要内容（≤200字），保留关键信息和数据点
3. **标签**：提取3-6个相关标签，用于分类检索。标签应涵盖文章涉及的主要技术领域、概念、工具等
4. **封面图建议**：可选，描述适合作为封面图的主题或场景（留空表示无特殊要求）
5. **字数**：统计原始正文的中文+英文总字数

## 约束条件
- 标题必须准确反映文章主旨，不夸大不缩小
- 摘要必须包含具体的信息要点（数据、结论、方法），不要空洞概括
- 标签应包含技术栈关键词 + 领域关键词的组合
- 不要编造原文中没有的信息"""


def _count_words(content: str) -> int:
    """Count total words in content (Chinese chars + English words).

    Chinese characters are counted individually; English words are split by whitespace.

    Args:
        content: Text content to count.

    Returns:
        Total word count.
    """
    # Count Chinese characters
    chinese_count = len([c for c in content if '\u4e00' <= c <= '\u9fff'])
    # Count English words (split by whitespace, filter non-empty)
    english_parts = re_sub_result = __import__('re').sub(
        r'[\u4e00-\u9fff]', ' ', content
    ).split()
    english_count = len([w for w in english_parts if w.strip()])
    return chinese_count + english_count


def _estimate_reading_time(word_count: int) -> str:
    """根据字数估算阅读时间。

    中文读者阅读技术内容平均约500字/分钟。

    参数：
        word_count: 总字数。

    返回：
        人类可读的字符串，如"3 min read"或"5 min read"。
    """
    if word_count == 0:
        return "0 min read"
    # Average reading speed for mixed Chinese-English tech content
    minutes = math.ceil(word_count / 500)
    if minutes < 1:
        return "<1 min read"
    return f"{minutes} min read"


def _extract_existing_h1_title(content: str) -> str | None:
    """Extract the first H1 heading from markdown content.

    Args:
        content: Markdown text.

    Returns:
        H1 title string if found, None otherwise.
    """
    import re
    match = re.match(r'^#\s+(.+)$', content, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# Main node function
# ---------------------------------------------------------------------------

def summary_meta_node(state: AgentState) -> Dict[str, Any]:
    """使用LLM从格式化内容生成结构化元数据。

    这是整个流水线中唯一的LLM调用。它使用gpt-4o-mini
    配合Pydantic结构化输出，以可靠地提取标题、摘要、
    标签和其他元数据。

    节点签名遵循C1约定：(state: AgentState) -> dict

    参数：
        state: 包含来自FormatOptimizeNode的formatted_content的代理状态。

    返回：
        部分状态更新，包含：
        - title: 提取或生成的文章标题。
        - summary: 文章摘要。
        - tags: 主题标签列表。
        - word_count: 内容字数。
        - reading_time: 估计阅读时间。

    异常：
        LLMError: 如果LLM API调用失败。
    """
    trace_logger = get_trace_logger()
    trace_id = state.get("run_log", {}).get("trace_id", "")
    trace_logger.node_enter("summary_meta", trace_id)

    try:
        formatted_content = state.get("formatted_content", "")
        if not formatted_content:
            raise LLMError("No formatted_content available for summarization")

        log = __import__("loguru").logger

        # Check for existing H1 title
        existing_title = _extract_existing_h1_title(formatted_content)

        # Build user message
        user_message = f"""请分析以下 Markdown 文章内容并提取元数据：

{formatted_content[:8000]}
{"\n\n注意：原文已有 H1 标题，请直接使用该标题。" if existing_title else ""}
"""

        # Get configured model
        model = get_summary_model()
        structured_model = model.with_structured_output(
            SummaryMetaOutput, method="function_calling"
        )

        log.bind(trace_id=trace_id).debug("Calling LLM for summary/metadata generation...")

        # Call LLM
        messages = [
            SystemMessage(content=SUMMARY_SYSTEM_PROMPT),
            HumanMessage(content=user_message),
        ]

        result: SummaryMetaOutput = structured_model.invoke(messages)

        # If original had an H1, prefer it over LLM-generated title
        final_title = existing_title or result.title

        # Calculate word count and reading time
        word_count = result.word_count if result.word_count > 0 else _count_words(formatted_content)
        reading_time = _estimate_reading_time(word_count)

        output_data = {
            "title": final_title,
            "summary": result.summary,
            "tags": result.tags,
            "word_count": word_count,
            "reading_time": reading_time,
        }

        log.bind(trace_id=trace_id).info(
            f"Summary/meta generated: title='{final_title}', "
            f"tags={result.tags}, words={word_count}, "
            f"reading_time={reading_time}"
        )

        return output_data

    except LLMError:
        raise
    except Exception as e:
        log = __import__("loguru").logger
        log.bind(trace_id=trace_id).error(f"LLM summary failed: {e}")
        raise LLMError(f"Summary/metadata generation failed: {e}") from e
    finally:
        trace_logger.node_exit("summary_meta", trace_id)
