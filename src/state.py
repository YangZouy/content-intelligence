"""
Agent State Definition - TypedDict-based pipeline state.

Defines the complete state shape passed between LangGraph nodes.
The state is partitioned into logical phases, each node writing
only its own phase's fields (C2 rule).

State Phases:
    1. InputState       - Raw input from CLI / ingest
    2. FormattedState   - After format optimization
    3. MetaState        - After LLM summary/metadata generation
    4. ImageProcessedState - After OSS image upload
    5. AdaptState       - After platform-specific content adaptation
    6. QualityState     - After deterministic pre-publish validation
    + RunLogEntry       - Cross-cutting observability data
"""

from typing import Any, Dict, List, Literal, Optional, TypedDict


# ---------------------------------------------------------------------------
# Image Reference
# ---------------------------------------------------------------------------

class ImageRef(TypedDict, total=False):
    """Reference to an image found during ingestion.

    Attributes:
        url_or_path: Local file path or remote URL of the image.
        alt: Alt text for the image (from markdown or inferred).
        usage: Whether this is an inline image or the cover image.
    """
    url_or_path: str
    alt: str
    usage: Literal["inline", "cover"]


# ---------------------------------------------------------------------------
# Brand Configuration
# ---------------------------------------------------------------------------

class BrandConfig(TypedDict, total=False):
    """Brand configuration applied during publishing.

    Attributes:
        name: Brand name (e.g., 'TechInsight').
        audience: Target audience description.
        tone: Content tone guideline.
        default_categories: Default Hexo categories list.
        default_tags_prefix: Default tag prefix list.
        author: Article author (wenyan front-matter `author` field).
        source_url: Original article URL (wenyan front-matter `source_url`).
    """
    name: str
    audience: str
    tone: str
    default_categories: List[str]
    default_tags_prefix: List[str]
    author: str
    source_url: str


# ---------------------------------------------------------------------------
# Phase 1: Input State (set by IngestNode)
# ---------------------------------------------------------------------------

class InputState(TypedDict, total=False):
    source_type: Literal["obsidian", "markdown_link"]
    raw_content: str
    images: List[ImageRef]
    file_path: str
    requested_platforms: List[str]
    brand: BrandConfig


# ---------------------------------------------------------------------------
# Phase 2: Formatted State (set by FormatOptimizeNode)
# ---------------------------------------------------------------------------

class FormattedState(TypedDict, total=False):
    """Content after formatting optimization.

    By default this is pure rule-engine output (zero LLM cost). When
    config.format_optimize.mode == "llm", an optional LLM semantic-polish
    pass is layered on top of the rule output (with a safety fallback).
    Knowledge content is preserved; only layout/format changes are made.
    """
    formatted_content: str


# ---------------------------------------------------------------------------
# Phase 3: Meta State (set by SummaryMetaNode)
# ---------------------------------------------------------------------------

class MetaState(TypedDict, total=False):
    """AI-generated metadata from LLM summary call.

    This is the ONLY place where LLM output enters the pipeline.
    """
    title: str
    summary: str
    tags: List[str]
    word_count: int
    reading_time: str


# ---------------------------------------------------------------------------
# Phase 4: Image Processed State (set by ImageProcessNode)
# ---------------------------------------------------------------------------

class ImageProcessedState(TypedDict, total=False):
    """Content with all images replaced by OSS URLs.

    Also includes cover URL and mapping information.
    """
    content_with_oss_images: str
    cover_url: str
    image_mapping: Dict[str, str]  # original -> oss_url
    oss_image_count: int


# ---------------------------------------------------------------------------
# Phase 5: Adapt State (set by ContentAdaptNode)
# ---------------------------------------------------------------------------

class AdaptState(TypedDict, total=False):
    """Platform-specific formatted documents ready for publishing.

    Each key contains the full document for that platform.
    """
    hexo_document: str
    wechat_draft: str


# ---------------------------------------------------------------------------
# Phase 6: Quality Gate State (set by QualityCheckNode)
# ---------------------------------------------------------------------------

class QualityIssue(TypedDict, total=False):
    """A deterministic quality-gate finding."""
    code: str
    message: str
    field: Optional[str]
    platform: Optional[str]


class QualityState(TypedDict, total=False):
    """Result used by the graph to route content before publishing."""
    quality_passed: bool
    quality_issues: List[QualityIssue]
    quality_check_count: int


# ---------------------------------------------------------------------------
# Publish Result Item (set by PublishNode)
# ---------------------------------------------------------------------------

class PublishResultItem(TypedDict, total=False):
    """Result of a single platform publish attempt.

    Each platform produces one item per run (after retries).
    """
    platform: str
    success: bool
    url: Optional[str]
    attempt: int
    error: Optional[str]


# ---------------------------------------------------------------------------
# Run Log Entry (cross-cutting, written throughout)
# ---------------------------------------------------------------------------

class RunLogEntry(TypedDict, total=False):
    """Complete log entry for one pipeline execution.

    Written as JSON to runs/<timestamp>.json at end of each run.
    """
    trace_id: str
    timestamp: str
    source_type: str
    file_name: str
    platforms_requested: List[str]
    node_durations: Dict[str, float]
    token_usage: Dict[str, int]
    publish_results: List[PublishResultItem]
    total_duration_seconds: float
    oss_uploads: List[Dict[str, str]]


# ---------------------------------------------------------------------------
# Complete Agent State (union of all phases)
# ---------------------------------------------------------------------------

class AgentState(
    InputState,
    FormattedState,
    MetaState,
    ImageProcessedState,
    AdaptState,
    QualityState,
    TypedDict,
    total=False,
):
    """
    total=False表示每个字段都不是必填的
    """
    publish_results: List[PublishResultItem]
    run_log: RunLogEntry
