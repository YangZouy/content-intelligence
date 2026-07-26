"""
发布节点 - 并行平台发布编排器。

协调多个发布器实现，将内容同时（或独立地）
发布到所有请求的平台。

关键设计：
- 每个发布器独立运行（一个失败不阻塞其他发布器）
- 每个发布器有自己的重试逻辑（指数退避，最多2次重试）
- 结果被收集并作为PublishResultItem列表返回

输出：{publish_results: [...]}
"""

from __future__ import annotations

import asyncio
import traceback
from typing import Any, Dict, List, Optional

from src.config_loader import get_config
from src.errors import CIDError
from src.state import AgentState, BrandConfig, PublishResultItem
from src.observability import get_trace_logger


# ---------------------------------------------------------------------------
# Publisher registry - maps platform keys to implementation classes
# ---------------------------------------------------------------------------

def _get_publisher_for_platform(platform_key: str):
    if platform_key == "blog":
        from src.publishers.github_pages import GitHubPagesPublisher
        return GitHubPagesPublisher()
    elif platform_key == "wechat":
        from src.publishers.wechat import WeChatPublisher
        return WeChatPublisher()
    else:
        raise ValueError(f"Unknown platform: {platform_key}. "
                        f"Supported: 'blog', 'wechat'")

# 发布单个平台
def _run_single_publish(
    publisher,
    content: str,
    brand: BrandConfig,
    max_retries: int = 2,
) -> PublishResultItem:
    import time as _time
    log = __import__("loguru").logger

    last_result: Optional[PublishResultItem] = None
    last_error: Optional[str] = None

    for attempt in range(1, max_retries + 2):  # initial + retries
        try:
            result = publisher.publish(content, brand)
            log.info(
                f"[{publisher.platform}] Publish succeeded on attempt {attempt}: "
                f"url={result.get('url', 'N/A')}"
            )
            return result

        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            log.warning(
                f"[{publisher.platform}] Attempt {attempt} failed: {e}"
            )

            # Build failure result for this attempt
            last_result = PublishResultItem(
                platform=publisher.platform,
                success=False,
                url=None,
                attempt=attempt,
                error=last_error,
            )

            # Wait before retry (exponential backoff)
            if attempt <= max_retries:
                delay = min(2 ** (attempt), 4)  # 1s, 2s, 4s capped at 4s
                log.debug(
                    f"[{publisher.platform}] Retrying in {delay}s..."
                )
                _time.sleep(delay)

    # All attempts exhausted — return last failure result
    assert last_result is not None
    return last_result


# ---------------------------------------------------------------------------
# Main node function
# ---------------------------------------------------------------------------

def publish_node(state: AgentState) -> Dict[str, Any]:
    trace_logger = get_trace_logger()
    trace_id = state.get("run_log", {}).get("trace_id", "")
    trace_logger.node_enter("publish", trace_id)

    try:
        # Get configuration
        config = get_config()
        max_retries = config.wechat.get("max_retries", 2)
        brand: BrandConfig = state.get("brand", {})  # type: ignore[assignment]

        # Determine target platforms
        requested_platforms: List[str] = state.get(
            "requested_platforms",
            config.default_options.get("platforms", ["blog", "wechat"]),
        )

        if not requested_platforms:
            raise CIDError("No platforms requested for publishing")

        log = __import__("loguru").logger
        log.bind(trace_id=trace_id).info(
            f"Publishing to platforms: {requested_platforms}"
        )

        # Content mapping: platform key -> state field
        content_map = {
            "blog": state.get("hexo_document", ""),
            "wechat": state.get("wechat_draft", ""),
        }

        # Execute publishers
        results: List[PublishResultItem] = []

        for platform_key in requested_platforms:
            try:
                publisher = _get_publisher_for_platform(platform_key)
                content = content_map.get(platform_key, "")

                if not content:
                    log.bind(trace_id=trace_id).warning(
                        f"No content available for platform '{platform_key}', skipping"
                    )
                    results.append(PublishResultItem(
                        platform=platform_key,
                        success=False,
                        url=None,
                        attempt=0,
                        error="No content available for this platform",
                    ))
                    continue

                # Validate before publishing
                is_valid, errors = publisher.validate(content, brand)
                if not is_valid:
                    log.bind(trace_id=trace_id).error(
                        f"Validation failed for '{platform_key}': {errors}"
                    )
                    results.append(PublishResultItem(
                        platform=platform_key,
                        success=False,
                        url=None,
                        attempt=0,
                        error=f"Validation failed: {'; '.join(errors)}",
                    ))
                    continue

                # Execute publish with retry
                result = _run_single_publish(
                    publisher, content, brand, max_retries=max_retries
                )
                results.append(result)

            except Exception as e:
                log.bind(trace_id=trace_id).error(
                    f"Unexpected error publishing to '{platform_key}': "
                    f"{traceback.format_exc()}"
                )
                results.append(PublishResultItem(
                    platform=platform_key,
                    success=False,
                    url=None,
                    attempt=0,
                    error=f"Unexpected error: {e}",
                ))

        # Log summary
        success_count = sum(1 for r in results if r.get("success"))
        total_count = len(results)
        log.bind(trace_id=trace_id).info(
            f"Publish complete: {success_count}/{total_count} platforms succeeded"
        )

        return {"publish_results": results}

    except CIDError:
        raise
    except Exception as e:
        log = __import__("loguru").logger
        log.bind(trace_id=trace_id).error(f"Publish orchestration failed: {e}")
        raise  # Re-raise generic errors
    finally:
        try:
            from src.publishers.wechat import WeChatPublisher
        except ImportError:
            pass

        trace_logger.node_exit("publish", trace_id)
