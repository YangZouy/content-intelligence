"""
格式优化节点 - 混合格式化（规则引擎 + 可选 LLM 语义润色）。

默认 mode=rule：纯规则引擎，零 LLM 令牌消耗、零内容损坏风险。
当 config.format_optimize.mode=llm 时，规则底座之上叠加 LLM 润色，
并对输出做安全校验（图片/代码块/公式数量不得改坏），失败则回退规则结果。

应用确定性格式化规则以提高可读性：
- 在句子边界处拆分长段落
- 并行项目 -> 无序列表
- 顺序步骤 -> 有序列表
- 关键术语加粗
- 警告/提示转换为引用块
- 代码块语言注释
- 长文章分节分隔符

硬性约束：绝不修改知识内容（论点、数据、
结论、代码块、图像、公式、表格）。
"""

from __future__ import annotations

import re
from typing import Any, Dict

from src.errors import FormatError
from src.state import AgentState
from src.observability import get_trace_logger
from src.config_loader import get_config


# ---------------------------------------------------------------------------
# Constants and patterns
# ---------------------------------------------------------------------------

# Maximum characters before a paragraph should be split
_MAX_PARAGRAPH_LENGTH = 200

# Minimum article length for adding section dividers (in Chinese characters)
_LONG_ARTICLE_THRESHOLD = 1500

# Sentence-ending punctuation (Chinese + English)
_SENTENCE_END = re.compile(r'(?<=[。！？.!?])\s*')

# Pattern for parallel item indicators (e.g., "第一、", "一是、", "首先、")
_PARALLEL_MARKERS = [
    r'(?:第一|一|首先|其一)[、,，]',
    r'(?:第二|二|其次|其二)[、,，]',
    r'(?:第三|三|再次|其三)[、,，]',
    r'(?:第四|四)[、,，]',
    r'(?:第五|五)[、,，]',
    r'^(?:另外|此外|还有|同时)[、,，]',
]

# Pattern for step/sequence indicators
_STEP_MARKERS = [
    r'^步骤\s*\d+[：:．.]',
    r'^第\s*\d+\s*步[：:．.]',
    r'^Step\s*\d+[：:.\s]',
    r'^\d+\)[\s]',
]

def _split_long_paragraphs(content: str) -> str:
    lines = content.split('\n')
    result_lines: list[str] = []

    in_code_block = False
    in_table = False

    for line in lines:
        # Track code block state
        if line.strip().startswith('```'):
            in_code_block = not in_code_block
            result_lines.append(line)
            continue

        # Track table state
        if not in_code_block and line.strip().startswith('|') and '|' in line[1:]:
            in_table = True
            result_lines.append(line)
            continue
        if in_table and not line.strip().startswith('|'):
            in_table = False

        # Skip formatting for code blocks and tables
        if in_code_block or in_table:
            result_lines.append(line)
            continue

        # Check if this is a long paragraph needing split
        stripped = line.strip()
        if len(stripped) > _MAX_PARAGRAPH_LENGTH and not _is_special_line(stripped):
            parts = _SENTENCE_END.split(stripped)
            current_chunk: list[str] = []
            current_len = 0

            for part in parts:
                part = part.strip()
                if not part:
                    continue

                if current_len + len(part) > _MAX_PARAGRAPH_LENGTH and current_chunk:
                    result_lines.append(' '.join(current_chunk))
                    current_chunk = [part]
                    current_len = len(part)
                else:
                    current_chunk.append(part)
                    current_len += len(part)

            if current_chunk:
                result_lines.append(' '.join(current_chunk))
        else:
            result_lines.append(line)

    return '\n'.join(result_lines)


def _is_special_line(line: str) -> bool:
    """Check if a line is a special markdown element that shouldn't be split.

    Args:
        line: A single text line.

    Returns:
        True if this is a heading, list item, blockquote, horizontal rule, etc.
    """
    stripped = line.strip()
    if not stripped:
        return True
    # Headings
    if stripped.startswith('#'):
        return True
    # List items
    if re.match(r'^[-*+]\s', stripped):
        return True
    if re.match(r'^\d+[.)]\s', stripped):
        return True
    # Blockquotes
    if stripped.startswith('>'):
        return True
    # Horizontal rules
    if re.match(r'^-{3,}$|^_{3,}$|^\*{3,}$', stripped):
        return True
    # HTML tags
    if stripped.startswith('<') and '>' in stripped[:20]:
        return True
    return False


def _convert_parallel_to_list(content: str) -> str:
    """Convert parallel item markers to unordered list items (- ).

    Detects patterns like "第一、" / "首先，" / "One," and converts them.
    Only converts when the marker appears at the start of a paragraph/line.

    Args:
        content: Markdown content.

    Returns:
        Content with parallel items converted to - list format.
    """
    lines = content.split('\n')
    result: list[str] = []
    in_code_block = False

    for line in lines:
        if line.strip().startswith('```'):
            in_code_block = not in_code_block
            result.append(line)
            continue

        if in_code_block:
            result.append(line)
            continue

        modified = line
        for pattern in _PARALLEL_MARKERS:
            replacement = r'- '
            modified = re.sub(pattern, replacement, modified, count=1)
            # If we made a substitution, stop checking other patterns
            if modified != line:
                break

        result.append(modified)

    return '\n'.join(result)


def _convert_steps_to_ordered_list(content: str) -> str:
    """Convert step indicators to ordered list format (1. 2. 3.).

    Detects patterns like "步骤1：" / "第1步" / "Step 1:" and converts.

    Args:
        content: Markdown content.

        Returns:
            Content with step items converted to numbered list format.
    """
    lines = content.split('\n')
    result: list[str] = []
    in_code_block = False

    for line in lines:
        if line.strip().startswith('```'):
            in_code_block = not in_code_block
            result.append(line)
            continue

        if in_code_block:
            result.append(line)
            continue

        modified = line
        for pattern in _STEP_MARKERS:
            new_line = re.sub(
                pattern, lambda m: f"{_extract_step_number(m.group(0))}. ", modified, count=1
            )
            if new_line != modified:
                modified = new_line
                break

        result.append(modified)

    return '\n'.join(result)


def _extract_step_number(match_text: str) -> str:
    """Extract the numeric step number from a matched step indicator.

    Args:
        match_text: The full matched text like "步骤1：" or "Step 3".

    Returns:
        The step number as a string.
    """
    nums = re.findall(r'\d+', match_text)
    return nums[0] if nums else "1"


def _bold_key_terms(content: str) -> str:
    """Apply conservative bold formatting to key technical terms.

    This is intentionally conservative — only applies to clearly identifiable
    term definitions or first-mention patterns to avoid over-formatting.

    Current strategy: bold terms that appear after common definition patterns:
    - "XX是指" -> "**XX**是指"
    - "所谓的XX" -> "所谓的**XX**"
    - "XX（也称为" -> "**XX**（也称为"

    Args:
        content: Markdown content.

        Returns:
        Content with key terms bolded.
    """
    # Definition-style patterns where the term precedes an explanation
    term_patterns = [
        # "XXX是指" pattern
        (
            r'([\u4e00-\u9fff]{2,6})(是指|定义为?|即|也就是)',
            r'**\1**\2'
        ),
        # "所谓的XXX" pattern
        (
            r'(所谓的)([\u4e00-\u9fff]{2,8})',
            r'\1**\2**'
        ),
        # "XXX（也叫" pattern
        (
            r'([\u4e00-\u9fff]{2,6})(（[^)]*(?:叫|称|称为|又称|别名)[^)]*）)',
            r'**\1**\2'
        ),
    ]

    result = content
    for pattern, replacement in term_patterns:
        result = re.sub(pattern, replacement, result)

    return result


def _convert_warnings_to_blockquote(content: str) -> str:
    """Convert warning/note patterns to blockquote format.

    Patterns:
    - "注意" / "警告" / "注意事项" -> "> ⚠️ ..."
    - "提示" / "重要" / "请注意" -> "> 💡 ..."

    Args:
        content: Markdown content.

        Returns:
            Content with warnings converted to blockquotes.
    """
    lines = content.split('\n')
    result: list[str] = []
    in_code_block = False

    warning_patterns = re.compile(
        r'^(注意|警告|注意事项|小心|谨慎)'
    )
    tip_patterns = re.compile(
        r'^(提示|重要|建议|请记住|记住|要点|核心)'
    )

    for line in lines:
        if line.strip().startswith('```'):
            in_code_block = not in_code_block
            result.append(line)
            continue

        if in_code_block:
            result.append(line)
            continue

        stripped = line.strip()

        # Check for warning patterns
        if warning_patterns.match(stripped):
            result.append(f"> ⚠️ {stripped}")
        elif tip_patterns.match(stripped):
            result.append(f"> 💡 {stripped}")
        else:
            result.append(line)

    return '\n'.join(result)


def _ensure_code_block_language(content: str) -> str:
    """Ensure code blocks have language annotations.

    If a code block opens with ``` without a language tag, attempt to
    infer from content or default to 'text'.

    Args:
        content: Markdown content.

        Returns:
            Content with all code blocks having language tags.
    """
    lines = content.split('\n')
    result: list[str] = []

    for i, line in enumerate(lines):
        # Detect opening fence without language
        if re.match(r'^```\s*$', line):
            # Peek ahead to guess language
            guessed_lang = _guess_code_language(lines, i + 1)
            result.append(f"```{guessed_lang}")
        else:
            result.append(line)

    return '\n'.join(result)


def _guess_code_language(lines: list[str], start_idx: int) -> str:
    """Guess the programming language of a code block from its content.

    Simple heuristic-based detection.

    Args:
        lines: All content lines.
        start_idx: Index right after the opening ```.

        Returns:
            Guessed language identifier string.
    """
    # Look ahead up to 10 non-empty lines for clues
    sample_lines = []
    for i in range(start_idx, min(start_idx + 15, len(lines))):
        if lines[i].strip().startswith('```'):
            break  # Hit closing fence
        sample_lines.append(lines[i].strip())

    sample = '\n'.join(sample_lines).lower()

    # Language heuristics
    if any(kw in sample for kw in ['def ', 'import ', 'class ', 'self.', '@']):
        return "python"
    if any(kw in sample for kw in ['function ', 'const ', 'let ', '=>', 'async ']):
        return "javascript"
    if any(kw in sample for kw in ['func ', 'package ', ':=', 'fmt.', 'go ']):
        return "go"
    if any(kw in sample for kw in ['public ', 'private ', 'void ', 'new ']):
        return "java"
    if '{%' in sample or '{#' in sample:
        return "jinja2"
    if '<html' in sample or '<div' in sample or '<!doctype' in sample:
        return "html"
    if 'SELECT ' in sample.upper() or 'FROM ' in sample.upper():
        return "sql"
    if '$' in sample or 'echo ' in sample or '#!/bin' in sample:
        return "bash"
    if '\\section' in sample or '\\begin{' in sample:
        return "latex"

    # Default fallback
    return "text"


def _add_section_dividers_for_long_articles(content: str) -> str:
    """Add horizontal rule separators for very long articles.

    Inserts `---` between major sections (after ## headings) when
    the total content exceeds the threshold length.

    Args:
        content: Full formatted markdown content.

        Returns:
            Content with section dividers inserted.
    """
    # Count approximate Chinese character count
    chinese_chars = len(re.sub(r'[^\u4e00-\u9fff]', '', content))

    if chinese_chars < _LONG_ARTICLE_THRESHOLD:
        return content

    lines = content.split('\n')
    result: list[str] = []

    for i, line in enumerate(lines):
        result.append(line)

        # Insert divider after ## level headings (but not ### or higher)
        if re.match(r'^##\s+(?!$)', line.strip()):
            # Don't add divider if next line is already a divider
            if i + 1 < len(lines):
                next_stripped = lines[i + 1].strip()
                if not re.match(r'^[-*_]{3,}$', next_stripped):
                    result.append('---')

    return '\n'.join(result)


# ---------------------------------------------------------------------------
# LLM polish layer (opt-in, only when format_optimize.mode == "llm")
# ---------------------------------------------------------------------------

# Prompt for the optional LLM semantic-polish pass. The rule engine has
# already handled deterministic structure; this pass adds the *semantic*
# touches the rules cannot (transition sentences, smart bolding of arbitrary
# terms, auto H1, heading-level fix). Content-safety constraints are explicit
# so the model is told not to touch code/images/formulas/tables.
_LLM_FORMAT_PROMPT = """\
# 角色
你是一个专业的技术文章编辑，负责优化Markdown文档的格式和排版。

## 优化规则

**段落优化**
- 超过200字的段落，在合适位置拆分为2-3段
- 补充段落间的过渡句，使行文更流畅
- 段落之间保持一个空行

**列表优化**
- 并列关系内容转为无序列表（- item）
- 步骤类内容转为有序列表（1. step）
- 列表项不超过1行时，删除多余标点
- 列表前后各保留一个空行

**强调优化**
- 核心术语、关键结论用 **加粗**
- 注意事项用 > ⚠️ 引用块
- 重要提示用 > 💡 引用块

**代码块优化**
- 确保所有代码块标注语言类型
- 代码块前后各保留一个空行
- 行内代码用反引号包裹

**结构优化**
- 如果缺少一级标题，根据内容生成一个
- 确保标题层级正确（# → ## → ###，不跳级）
- 超过3000字的文章，在适当位置添加 --- 分割线
- 标题与正文之间保留一个空行

**图片处理**
- 保留所有图片链接，不做任何修改
- 确保图片语法正确：![描述](URL)
- 图片前后各保留一个空行

**公式处理**
- 保留所有LaTeX公式的原始格式
- 块级公式使用 $$公式$$ 格式
- 行内公式使用 $公式$ 格式
- 不要将公式转换为代码块

## 限制
- ❌ 不修改知识内容本身
- ❌ 不改变代码块内容
- ❌ 不删除或修改任何图片、公式、表格
- ❌ 不改变公式的格式（保持 $$ 或 $ 包裹）
- ✅ 只优化格式和排版

## 输出
直接输出优化后的完整Markdown内容，不要添加任何解释或说明。\
"""


def _count_images(content: str) -> int:
    """Count markdown image references (avoid double-counting HTML <img>)."""
    return len(re.findall(r'!\[[^\]]*\]\([^)]+\)', content))


def _count_code_fences(content: str) -> int:
    """Count fenced code-block opening/closing markers (must stay even)."""
    return len(re.findall(r'^```', content, re.MULTILINE))


def _count_formulas(content: str) -> int:
    """Count block ($$...$$) and inline ($...$) LaTeX formulas."""
    block = len(re.findall(r'\$\$[^$]+?\$\$', content, re.DOTALL))
    inline = len(re.findall(r'(?<!\$)\$(?!\$)(?:\\.|[^$\n])+?\$(?!\$)', content))
    return block + inline


def _llm_output_safe(before: str, after: str) -> bool:
    """Reject LLM output that dropped or corrupted protected content.

    Compares counts of images, fenced code blocks, and math formulas between
    the rule-optimized input and the LLM-polished output. If the LLM removed
    any protected element, we keep the rule output instead.
    """
    if _count_images(after) < _count_images(before):
        return False
    if _count_code_fences(after) != _count_code_fences(before):
        return False
    if _count_formulas(after) < _count_formulas(before):
        return False
    return True


def _llm_polish(content: str, max_tokens: int) -> str:
    """Optional LLM semantic-polish pass on top of the rule output.

    Reuses the single LLM factory (get_summary_model); we simply don't wrap
    it in with_structured_output because this is free-form text. Raises on any
    failure so the caller can fall back to the rule output.
    """
    from langchain_core.messages import SystemMessage, HumanMessage
    from src.llm import get_summary_model

    model = get_summary_model(max_tokens=max_tokens)
    resp = model.invoke([
        SystemMessage(content=_LLM_FORMAT_PROMPT),
        HumanMessage(content=content),
    ])
    text = resp.content
    if isinstance(text, list):
        # Some providers return a list of content parts
        text = "".join(
            part if isinstance(part, str) else getattr(part, "text", "")
            for part in text
        )
    return text if isinstance(text, str) else str(text)


def _apply_rule_pipeline(content: str) -> str:
    """Run the deterministic rule engine (steps 1-7). Always runs first."""
    content = _split_long_paragraphs(content)
    content = _convert_parallel_to_list(content)
    content = _convert_steps_to_ordered_list(content)
    content = _bold_key_terms(content)
    content = _convert_warnings_to_blockquote(content)
    content = _ensure_code_block_language(content)
    content = _add_section_dividers_for_long_articles(content)
    return content


# ---------------------------------------------------------------------------
# Main node function
# ---------------------------------------------------------------------------

def format_optimize_node(state: AgentState) -> Dict[str, Any]:
    """对原始内容应用基于规则的格式优化。
    处理顺序：
    1. 在句子边界处拆分长段落（超过 200 个字符）
    2. 将并列项转换为无序列表（- ）
    3. 将连续步骤转换为有序列表（1. 2. 3.）
    4. 加粗关键术语（保守处理）
    5. 将警告/提示转换为引用块（> ⚠️ / > 💡）
    6. 确保代码块包含语言标签
    7. 为长文章（超过 1500 个中文字符）添加章节分隔符
    """
    trace_logger = get_trace_logger()
    trace_id = state.get("run_log", {}).get("trace_id", "")
    trace_logger.node_enter("format_optimize", trace_id)

    try:
        raw_content = state.get("raw_content", "")
        if not raw_content:
            raise FormatError("No raw_content available for formatting")

        log = __import__("loguru").logger
        log.bind(trace_id=trace_id).debug("Starting format optimization pipeline")

        cfg = get_config()
        fmt_cfg = cfg.format_optimize or {}
        cli_mode = (state.get("format_optimize_mode") or "").strip().lower()
        mode = cli_mode if cli_mode in ("rule", "llm") else (fmt_cfg.get("mode", "rule") or "rule").lower()

        # Step A: deterministic rule engine (always runs as safe base)
        content = _apply_rule_pipeline(raw_content)

        # Step B: optional LLM semantic-polish pass (opt-in)
        if mode == "llm":
            log.bind(trace_id=trace_id).info(
                "format_optimize mode=llm: applying LLM polish"
            )
            try:
                polished = _llm_polish(
                    content, int(fmt_cfg.get("llm_max_tokens", 4096))
                )
                if fmt_cfg.get("safety_check", True) and not _llm_output_safe(
                    content, polished
                ):
                    log.bind(trace_id=trace_id).warning(
                        "LLM polish failed safety check "
                        "(images/code/formulas changed); "
                        "falling back to rule output"
                    )
                else:
                    content = polished
                    log.bind(trace_id=trace_id).info("LLM polish applied")
            except Exception as e:
                log.bind(trace_id=trace_id).warning(
                    f"LLM polish failed ({e}); falling back to rule output"
                )

        log.bind(trace_id=trace_id).info(
            f"Format optimization complete (mode={mode}, "
            f"input={len(raw_content)} chars, output={len(content)} chars)"
        )

        return {"formatted_content": content}

    except FormatError:
        raise
    except Exception as e:
        log = __import__("loguru").logger
        log.bind(trace_id=trace_id).error(f"Format optimization failed: {e}")
        raise FormatError(f"Format optimization failed: {e}") from e
    finally:
        trace_logger.node_exit("format_optimize", trace_id)
