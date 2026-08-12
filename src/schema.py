from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, Field


class SummaryMetaOutput(BaseModel):
    """LLM摘要/元数据生成步骤的结构化输出。

    这是整个流水线中唯一的LLM调用。模型从格式化内容中
    提取标题、摘要、标签和元数据。

    属性：
        title: 中文文章标题（最长30个字符）。
        summary: 文章内容的简短摘要（最长200个字符）。
        tags: 3-6个相关主题标签。
        word_count: 原始内容的总字数。
    """
    title: str = Field(
        description="Chinese article title, maximum 30 characters"
    )
    summary: str = Field(
        description="Article abstract/summary, maximum 200 characters"
    )
    tags: List[str] = Field(
        description="3-6 relevant topic tags for categorization",
        min_length=1,
        max_length=8,
    )
    word_count: int = Field(
        description="Total word count of the original article content",
        ge=0,
    )


class HexoFrontMatter(BaseModel):
    """Front-matter data model shared by the Hexo blog post and the WeChat draft.

    This single model is reused for BOTH platforms (the blog and the WeChat
    draft "share the same front-matter md file"):

    - Hexo uses: title, date, tags, categories, layout, top_img, cover, description
    - Wenyan (微信公众号排版工具) reads from the same block: title (required),
      cover, author, source_url. Wenyan's documented front-matter fields are:
        - title       文章标题（必填）
        - cover       文章封面（本地路径或网络图片 URL）
        - author      文章作者
        - source_url  原文地址
        - type        图文 / 图片消息（小绿书）
        - image_list  图片消息的图片列表
        - need_open_comment / only_fans_can_comment  评论相关
      Wenyan simply ignores YAML keys it does not recognize (date, tags,
      categories, layout, ...), so a single shared block satisfies both.

    Attributes:
        title: Post title.
        date: ISO 8601 publication date.
        tags: List of post tags.
        categories: List of post categories.
        layout: Hexo layout template (default: 'post').
        top_img: URL for top banner image.
        cover: URL for cover/thumbnail image (also used by wenyan as 封面).
        description: Short description for SEO and listing pages.
        author: Article author (used by wenyan as 作者).
        source_url: Original article URL (used by wenyan as 原文地址).
    """
    title: str
    date: str
    tags: List[str] = Field(default_factory=list)
    categories: List[str] = Field(default_factory=list)
    layout: str = "post"
    top_img: str = ""
    cover: str = ""
    description: str = ""
    author: Optional[str] = None
    source_url: Optional[str] = None

    def to_yaml_string(self) -> str:
        """Serialize this front-matter to a YAML string.

        Returns:
            YAML-formatted front-matter ready to wrap in '---' delimiters.
        """
        import yaml

        data = self.model_dump(exclude_none=True)

        # Ensure date is in ISO format
        if isinstance(data.get("date"), datetime):
            data["date"] = data["date"].isoformat()

        yaml_str = yaml.dump(data, allow_unicode=True, default_flow_style=False)
        return yaml_str.strip()

    @classmethod
    def from_state(
        cls,
        title: str,
        tags: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        cover_url: str = "",
        summary: str = "",
        date_iso: Optional[str] = None,
        author: Optional[str] = None,
        source_url: Optional[str] = None,
    ) -> "HexoFrontMatter":
        """Factory method to create instance from pipeline state fields.

        Args:
            title: Article title from MetaState.
            tags: Tags list from MetaState.
            categories: Categories from brand config.
            cover_url: Cover image URL from ImageProcessedState.
            summary: Description from MetaState.
            date_iso: ISO format date string (defaults to now UTC).
            author: Article author (wenyan `author` field), from brand config.
            source_url: Original article URL (wenyan `source_url` field).

        Returns:
            Populated HexoFrontMatter instance.
        """
        return cls(
            title=title,
            date=date_iso or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            tags=tags or [],
            categories=categories or [],
            cover=cover_url,
            top_img=cover_url,
            description=summary,
            author=author,
            source_url=source_url,
        )
