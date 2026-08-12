"""Checkpoint-friendly, idempotent platform publishing steps."""

from __future__ import annotations

import traceback
from typing import Any, Dict, List, Optional

from src.config_loader import get_config
from src.observability import get_trace_logger
from src.publication_ledger import get_publication_ledger
from src.state import AgentState, BrandConfig, PublishResultItem


def build_idempotency_key(platform: str, article_id: str, content_version: str) -> str:
    """Human-readable composite key backed by the publication ledger."""
    return f"{article_id}:{platform}:{content_version}"


def _get_publisher_for_platform(platform_key: str):
    if platform_key == "blog":
        from src.publishers.github_pages import GitHubPagesPublisher
        return GitHubPagesPublisher()
    if platform_key == "wechat":
        from src.publishers.wechat import WeChatPublisher
        return WeChatPublisher()
    raise ValueError(f"Unknown platform: {platform_key}")


def _run_single_publish(
    publisher,
    content: str,
    brand: BrandConfig,
    idempotency_key: str,
    max_retries: int = 2,
) -> PublishResultItem:
    import time as _time

    log = __import__("loguru").logger
    last_result: Optional[PublishResultItem] = None
    for attempt in range(1, max_retries + 2):
        try:
            result = publisher.publish(content, brand)
            result["attempt"] = attempt
            result["idempotency_key"] = idempotency_key
            result["skipped"] = False
            return result
        except Exception as exc:
            last_result = PublishResultItem(
                platform=publisher.platform,
                success=False,
                url=None,
                attempt=attempt,
                error=f"{type(exc).__name__}: {exc}",
                idempotency_key=idempotency_key,
                skipped=False,
            )
            log.warning(f"[{publisher.platform}] Attempt {attempt} failed: {exc}")
            if attempt <= max_retries:
                delay = min(2 ** attempt, 4)
                log.debug(f"[{publisher.platform}] Retrying in {delay}s...")
                _time.sleep(delay)
    assert last_result is not None
    return last_result


def _merge_result(
    previous: List[PublishResultItem],
    current: PublishResultItem,
) -> List[PublishResultItem]:
    """Keep one latest result per platform while preserving platform order."""
    merged = [item for item in previous if item.get("platform") != current.get("platform")]
    merged.append(current)
    return merged


def _successful_result(
    results: List[PublishResultItem],
    platform: str,
    idempotency_key: str,
) -> Optional[PublishResultItem]:
    for result in results:
        if (
            result.get("platform") == platform
            and result.get("success")
            and result.get("idempotency_key") == idempotency_key
        ):
            return result
    return None


def publish_platform(state: AgentState, platform: str) -> Dict[str, Any]:
    """Publish one platform or reuse its checkpointed successful result."""
    trace_logger = get_trace_logger()
    trace_id = state.get("run_log", {}).get("trace_id", "")
    node_name = f"publish_{platform}"
    trace_logger.node_enter(node_name, trace_id)
    try:
        previous = list(state.get("publish_results", []))
        requested = state.get("requested_platforms", [])
        if platform not in requested:
            return {"publish_results": previous}

        content_field = "hexo_document" if platform == "blog" else "wechat_draft"
        content = state.get(content_field, "")
        article_id = state.get("article_id", "")
        content_version = state.get("content_version", "")
        if not article_id or not content_version:
            raise ValueError("article_id and content_version are required before publishing")
        key = build_idempotency_key(platform, article_id, content_version)
        completed = _successful_result(previous, platform, key)
        if completed:
            reused = PublishResultItem(**completed)
            reused["skipped"] = True
            return {"publish_results": _merge_result(previous, reused)}

        ledger = get_publication_ledger()
        ledger_entry = ledger.successful_publication(article_id, platform, content_version)
        if ledger_entry:
            reused = PublishResultItem(
                platform=platform,
                success=True,
                url=ledger_entry.get("external_id"),
                attempt=0,
                error=None,
                idempotency_key=key,
                skipped=True,
            )
            return {"publish_results": _merge_result(previous, reused)}

        if not content:
            result = PublishResultItem(
                platform=platform,
                success=False,
                url=None,
                attempt=0,
                error="No content available for this platform",
                idempotency_key=key,
                skipped=False,
            )
            return {"publish_results": _merge_result(previous, result)}

        publisher = _get_publisher_for_platform(platform)
        brand: BrandConfig = state.get("brand", {})  # type: ignore[assignment]
        is_valid, errors = publisher.validate(content, brand)
        if not is_valid:
            result = PublishResultItem(
                platform=platform,
                success=False,
                url=None,
                attempt=0,
                error=f"Validation failed: {'; '.join(errors)}",
                idempotency_key=key,
                skipped=False,
            )
            return {"publish_results": _merge_result(previous, result)}

        max_retries = get_config().wechat.get("max_retries", 2)
        result = _run_single_publish(publisher, content, brand, key, max_retries)
        if result.get("success"):
            ledger.record_success(
                article_id,
                platform,
                content_version,
                result.get("url"),
                state.get("publication_date", ""),
            )
        return {"publish_results": _merge_result(previous, result)}
    except Exception as exc:
        log = __import__("loguru").logger
        log.bind(trace_id=trace_id).error(
            f"Unexpected error publishing to '{platform}': {traceback.format_exc()}"
        )
        content_field = "hexo_document" if platform == "blog" else "wechat_draft"
        result = PublishResultItem(
            platform=platform,
            success=False,
            url=None,
            attempt=0,
            error=f"Unexpected error: {exc}",
            idempotency_key=build_idempotency_key(
                platform,
                state.get("article_id", "missing-article"),
                state.get("content_version", "missing-version"),
            ),
            skipped=False,
        )
        return {"publish_results": _merge_result(list(state.get("publish_results", [])), result)}
    finally:
        trace_logger.node_exit(node_name, trace_id)


def publish_blog_node(state: AgentState) -> Dict[str, Any]:
    return publish_platform(state, "blog")


def publish_wechat_node(state: AgentState) -> Dict[str, Any]:
    return publish_platform(state, "wechat")


def publish_node(state: AgentState) -> Dict[str, Any]:
    """Backward-compatible in-process wrapper used outside the graph."""
    after_blog = dict(state)
    after_blog.update(publish_blog_node(state))
    return publish_wechat_node(after_blog)
