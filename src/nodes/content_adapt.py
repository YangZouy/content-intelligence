"""
内容适配节点 - 平台特定内容格式化。

将已处理（OSS链接的）内容转换为平台特定格式：
1. **Hexo文档**：front-matter + 正文，可直接用于GitHub Pages博客
2. **微信草稿**：与博客公用同一份front-matter + 正文，用于微信公众号文章
   （wenyan 工具要求每篇 Markdown 顶部带 YAML front-matter，至少含 title；
    它会读取 title / cover / author / source_url，忽略 Hexo 专属字段）

输出：{hexo_document, wechat_draft}
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from src.schema import HexoFrontMatter
from src.state import AgentState
from src.observability import get_trace_logger


def _build_hexo_front_matter(state: AgentState) -> HexoFrontMatter:
    """从流水线状态构建front-matter。

    从状态中收集标题、标签、分类、封面URL、摘要、作者、原文地址，
    并组装成完整的HexoFrontMatter模型。该模型同时被博客(Hexo)和
    微信(wenyan)复用——即「微信和博客公用同一份front-matter」。

    参数：
        state: 元数据处理和图像处理阶段之后的代理状态。

    返回：
        已填充的HexoFrontMatter实例。
    """
    brand = state.get("brand", {})
    return HexoFrontMatter.from_state(
        title=state.get("title", "Untitled"),
        tags=state.get("tags", []),
        categories=brand.get("default_categories", ["Technology"]),
        cover_url=state.get("cover_url", ""),
        summary=state.get("summary", ""),
        author=brand.get("author"),
        source_url=brand.get("source_url"),
    )


def _strip_front_matter(content: str) -> str:
    """如果内容中存在现有的YAML front-matter，则将其移除。

    某些源文档可能已包含front-matter块。
    此操作将其移除，以避免在添加我们自己的front-matter时重复。

    参数：
        content: 可能包含front-matter的markdown内容。

    返回：
        移除front-matter后的内容。
    """
    import re
    # Match YAML front-matter block: --- ... ---
    pattern = r'^---\s*\n.*?\n---\s*\n?'
    result = re.sub(pattern, '', content, count=1, flags=re.DOTALL)
    return result


# ---------------------------------------------------------------------------
# Main node function
# ---------------------------------------------------------------------------

def content_adapt_node(state: AgentState) -> Dict[str, Any]:
    """将处理后的内容适配到每个目标平台的格式要求。

    生成两种平台特定的文档格式：

    1. **hexo_document**：
       - YAML front-matter（title、date、tags、categories、layout、cover）
       - 正文内容，包含OSS图像链接
       - 可直接写入 hexo/source/_posts/<title>.md

    2. **wechat_draft**：
       - 与博客公用同一份 front-matter（title、cover、author、source_url 等）
       - wenyan 工具要求每篇 Markdown 顶部带 front-matter（至少含 title）
       - 正文内容，包含OSS图像链接
       - 可直接通过 wenyan-mcp create_draft API 提交

    节点签名遵循C1约定：(state: AgentState) -> dict

    参数：
        state: 包含元数据、经OSS处理的内容和品牌配置的代理状态。

    返回：
        部分状态更新，包含：
        - hexo_document: 带front-matter的完整Hexo文章。
        - wechat_draft: 兼容微信的草稿内容。
    """
    trace_logger = get_trace_logger()
    trace_id = state.get("run_log", {}).get("trace_id", "")
    trace_logger.node_enter("content_adapt", trace_id)

    try:
        oss_content = state.get("content_with_oss_images", "")
        if not oss_content:
            # Fallback to formatted_content if no OSS processing happened
            oss_content = state.get("formatted_content", "")

        log = __import__("loguru").logger

        # --- Build shared front-matter (used by BOTH hexo and wechat) ---
        fm = _build_hexo_front_matter(state)
        fm_yaml = fm.to_yaml_string()
        body = _strip_front_matter(oss_content)

        # Hexo post: front-matter + body
        hexo_doc = f"---\n{fm_yaml}\n---\n\n{body}"

        log.bind(trace_id=trace_id).debug(f"Hexo document built ({len(hexo_doc)} chars)")

        # --- Build WeChat Draft ---
        # 微信与博客公用同一份 front-matter：wenyan 工具要求每篇 Markdown
        # 顶部带 YAML front-matter（至少含 title），否则无法正确上传。
        # 它读取 title / cover / author / source_url，并忽略 Hexo 专属字段
        # （date / tags / categories / layout / top_img / description）。
        wechat_draft = f"---\n{fm_yaml}\n---\n\n{body.strip()}"

        log.bind(trace_id=trace_id).debug(f"WeChat draft built ({len(wechat_draft)} chars)")

        return {
            "hexo_document": hexo_doc,
            "wechat_draft": wechat_draft,
        }

    except Exception as e:
        log = __import__("loguru").logger
        log.bind(trace_id=trace_id).error(f"Content adaptation failed: {e}")
        raise  # Re-raise as generic error (not domain-specific)
    finally:
        trace_logger.node_exit("content_adapt", trace_id)
