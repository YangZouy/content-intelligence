"""
发布前的确定性质量门禁：按明确规则判断内容能否进入后续发布流程
检查：
1、元数据是否合法（检查状态里的标题、摘要、标签）
标题不能为空，最长 30 字
摘要不能为空，最长 200 字
标签数量必须是 3～6 个
每个标签不能为空，最长 20 字
2、Front Matter 是否完整：Hexo 和微信草稿的 Markdown 顶部都应有 YAML Front Matter
文档是否以 --- 开始、并以正确的 --- 结束
YAML 是否能正常解析
Hexo 必需字段是否完整：title、date、tags、categories、layout、cover、description
微信必需字段是否完整：title、cover
Hexo 的 layout 是否为 post
微信草稿是否超过约 10 万字符
3、Markdown 结构是否损坏
代码块是否有未闭合的 ``` 或 ~~~
行间公式的 $$ 是否成对闭合
HTML 注释 <!-- ... --> 是否闭合
4、代码和公式是否在适配时丢失
检查器会将原始 Markdown 中的：
围栏代码块
行内公式，如 $E=mc^2$
块级公式，如 $$ ... $$
与 Hexo、微信适配后的正文逐一对照。
如果原文的代码块被改写、删掉，或公式在适配后消失，会生成如：
structure.code_block_lost
structure.formula_lost
后续有限修复节点可以根据这些问题重新生成一次内容。
5、OSS 图片是否处理完整
比对摄取阶段识别出的图片数量 images
图片上传映射 image_mapping
替换后的正文 content_with_oss_images
检查内容包括：
是否每张发现的图片都生成了 OSS 映射
正文是否还残留未替换的图片地址
每一个 OSS URL 是否确实写入了正文
6、按目标平台分别检查
如果用户只选择发布博客，微信草稿即使为空也不会导致失败；
反之亦然。这样检查结果只约束本次实际会发生副作用的平台。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

import yaml

from src.observability import get_trace_logger
from src.state import AgentState, QualityIssue


HEXO_REQUIRED_FIELDS = (
    "title",
    "date",
    "tags",
    "categories",
    "layout",
    "cover",
    "description",
)
WECHAT_REQUIRED_FIELDS = ("title", "cover")

_IMAGE_PATTERN = re.compile(
    r"!\[[^\]]*\]\(([^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)"
    r"|<img\b[^>]*\bsrc=['\"]([^'\"]+)['\"][^>]*>",
    re.IGNORECASE,
)
_FENCED_CODE_PATTERN = re.compile(r"(^|\n)(`{3,}|~{3,})[^\n]*\n.*?\n\2(?=\n|$)", re.DOTALL)
_BLOCK_MATH_PATTERN = re.compile(r"\$\$.*?\$\$|\\\[.*?\\\]", re.DOTALL)
_INLINE_MATH_PATTERN = re.compile(r"(?<!\\)(?<!\$)\$(?!\$|\s)(.+?)(?<!\s)(?<!\\)\$(?!\$)")


def _issue(
    code: str,
    message: str,
    *,
    field: str | None = None,
    platform: str | None = None,
) -> QualityIssue:
    return QualityIssue(code=code, message=message, field=field, platform=platform)


def _front_matter(document: str) -> Tuple[Mapping[str, Any] | None, str, str | None]:
    normalized = document.lstrip("\ufeff")
    match = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", normalized, re.DOTALL)
    if not match:
        return None, normalized, "document must start with a closed YAML front matter block"
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return None, normalized[match.end():], f"front matter is invalid YAML: {exc}"
    if not isinstance(parsed, dict):
        return None, normalized[match.end():], "front matter must be a YAML mapping"
    return parsed, normalized[match.end():], None


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def _extract_images(content: str) -> List[str]:
    return [match.group(1) or match.group(2) for match in _IMAGE_PATTERN.finditer(content)]


def _extract_code_blocks(content: str) -> List[str]:
    return [match.group(0).lstrip("\n") for match in _FENCED_CODE_PATTERN.finditer(content)]


def _extract_formulas(content: str) -> List[str]:
    blocks = [match.group(0) for match in _BLOCK_MATH_PATTERN.finditer(content)]
    without_blocks = _BLOCK_MATH_PATTERN.sub("", content)
    return blocks + [match.group(0) for match in _INLINE_MATH_PATTERN.finditer(without_blocks)]


def _missing_fragments(source: Iterable[str], target: str) -> int:
    return sum(1 for fragment in source if fragment not in target)


def _check_markdown_closed(content: str, platform: str) -> List[QualityIssue]:
    issues: List[QualityIssue] = []
    fence_stack: List[Tuple[str, int]] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if not match:
            continue
        marker = match.group(1)
        if not fence_stack:
            fence_stack.append((marker[0], line_number))
        elif fence_stack[-1][0] == marker[0]:
            fence_stack.pop()
    if fence_stack:
        issues.append(_issue(
            "markdown.unclosed_code_fence",
            f"code fence opened near line {fence_stack[-1][1]} is not closed",
            platform=platform,
        ))

    if len(re.findall(r"(?<!\\)\$\$", content)) % 2:
        issues.append(_issue(
            "markdown.unclosed_math_block",
            "display-math delimiter '$$' is not closed",
            platform=platform,
        ))
    if content.count("<!--") != content.count("-->"):
        issues.append(_issue(
            "markdown.unclosed_html_comment",
            "HTML comment delimiter is not closed",
            platform=platform,
        ))
    return issues


def _check_metadata(state: AgentState) -> List[QualityIssue]:
    issues: List[QualityIssue] = []
    title = state.get("title", "").strip()
    summary = state.get("summary", "").strip()
    tags = state.get("tags", [])

    if not title:
        issues.append(_issue("metadata.title_missing", "title is required", field="title"))
    elif len(title) > 30:
        issues.append(_issue("metadata.title_too_long", "title must not exceed 30 characters", field="title"))
    if not summary:
        issues.append(_issue("metadata.summary_missing", "summary is required", field="summary"))
    elif len(summary) > 200:
        issues.append(_issue("metadata.summary_too_long", "summary must not exceed 200 characters", field="summary"))
    if not isinstance(tags, list) or not 3 <= len(tags) <= 6:
        issues.append(_issue("metadata.tags_count", "tags must contain 3 to 6 items", field="tags"))
    elif any(not isinstance(tag, str) or not tag.strip() or len(tag.strip()) > 20 for tag in tags):
        issues.append(_issue("metadata.tag_invalid", "each tag must contain 1 to 20 characters", field="tags"))
    return issues


def _check_document(
    document: str,
    platform: str,
    required_fields: Sequence[str],
) -> Tuple[List[QualityIssue], str]:
    issues: List[QualityIssue] = []
    if not document.strip():
        return [_issue("document.empty", "adapted document is empty", platform=platform)], ""

    front_matter, body, error = _front_matter(document)
    if error:
        issues.append(_issue("front_matter.invalid", error, platform=platform))
    else:
        assert front_matter is not None
        for field in required_fields:
            if field not in front_matter or _is_empty(front_matter[field]):
                issues.append(_issue(
                    "front_matter.missing_field",
                    f"required front matter field '{field}' is missing or empty",
                    field=field,
                    platform=platform,
                ))
        if front_matter.get("layout") not in (None, "post") and platform == "blog":
            issues.append(_issue(
                "hexo.invalid_layout",
                "Hexo layout must be 'post'",
                field="layout",
                platform=platform,
            ))
    issues.extend(_check_markdown_closed(body, platform))
    if platform == "wechat" and len(document) > 100_000:
        issues.append(_issue(
            "wechat.content_too_long",
            "WeChat document exceeds 100,000 characters",
            platform=platform,
        ))
    return issues, body


def run_quality_checks(state: AgentState) -> List[QualityIssue]:
    """Return all deterministic findings without performing external calls."""
    issues = _check_metadata(state)
    requested = state.get("requested_platforms", ["blog", "wechat"])
    bodies: Dict[str, str] = {}

    if "blog" in requested:
        document_issues, bodies["blog"] = _check_document(
            state.get("hexo_document", ""), "blog", HEXO_REQUIRED_FIELDS
        )
        issues.extend(document_issues)
    if "wechat" in requested:
        document_issues, bodies["wechat"] = _check_document(
            state.get("wechat_draft", ""), "wechat", WECHAT_REQUIRED_FIELDS
        )
        issues.extend(document_issues)

    raw_content = state.get("raw_content", "")
    source_code = _extract_code_blocks(raw_content)
    source_formulas = _extract_formulas(raw_content)
    for platform, body in bodies.items():
        missing_code = _missing_fragments(source_code, body)
        if missing_code:
            issues.append(_issue(
                "structure.code_block_lost",
                f"{missing_code} source code block(s) were changed or lost",
                platform=platform,
            ))
        missing_formulas = _missing_fragments(source_formulas, body)
        if missing_formulas:
            issues.append(_issue(
                "structure.formula_lost",
                f"{missing_formulas} source formula(s) were changed or lost",
                platform=platform,
            ))

    images = state.get("images", [])
    mapping = state.get("image_mapping", {})
    if len(mapping) < len(images):
        issues.append(_issue(
            "oss.upload_incomplete",
            f"only {len(mapping)} of {len(images)} discovered image(s) have OSS mappings",
            field="image_mapping",
        ))

    processed_content = state.get("content_with_oss_images", "")
    mapped_urls = set(mapping.values())
    unresolved = [url for url in _extract_images(processed_content) if url not in mapped_urls]
    if unresolved:
        issues.append(_issue(
            "oss.unresolved_image",
            f"{len(unresolved)} image reference(s) were not replaced by mapped OSS URLs",
            field="content_with_oss_images",
        ))
    for original, oss_url in mapping.items():
        if oss_url not in processed_content:
            issues.append(_issue(
                "oss.replacement_missing",
                f"OSS replacement for '{original}' is absent from processed content",
                field="content_with_oss_images",
            ))

    return issues


def quality_check_node(state: AgentState) -> Dict[str, Any]:
    """LangGraph node wrapper around the deterministic quality rules."""
    trace_logger = get_trace_logger()
    trace_id = state.get("run_log", {}).get("trace_id", "")
    trace_logger.node_enter("quality_check", trace_id)
    try:
        issues = run_quality_checks(state)
        return {
            "quality_passed": not issues,
            "quality_issues": issues,
            "quality_check_count": state.get("quality_check_count", 0) + 1,
        }
    finally:
        trace_logger.node_exit("quality_check", trace_id)
