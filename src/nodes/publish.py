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
    """工厂函数，用于获取给定平台的发布器实例。

    参数：
        platform_key: 平台标识符（'blog' 或 'wechat'）。

    返回：
        指定平台的发布器实例。

    异常：
        ValueError: 如果平台标识符未被识别。
    """
    if platform_key == "blog":
        from src.publishers.github_pages import GitHubPagesPublisher
        return GitHubPagesPublisher()
    elif platform_key == "wechat":
        from src.publishers.wechat import WeChatPublisher
        return WeChatPublisher()
    else:
        raise ValueError(f"Unknown platform: {platform_key}. "
                        f"Supported: 'blog', 'wechat'")


def _run_single_publish(
    publisher,
    content: str,
    brand: BrandConfig,
    max_retries: int = 2,
) -> PublishResultItem:
    """Execute publish + retry for a single platform.

    Wraps the publisher's publish() call in retry logic with
    exponential backoff (1s -> 2s -> 4s).

    The retry is implemented inline here rather than relying solely on
    the @retry_with_backoff decorator, giving us finer control over
    error handling and result collection per attempt.

    Args:
        publisher: Publisher instance.
        content: Platform-formatted content.
        brand: Brand configuration.
        max_retries: Maximum number of additional retry attempts.

    Returns:
        Final PublishResultItem after all attempts.
    """
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
    """将适配后的内容发布到所有请求的平台。

    本节点实现C5规则：**独立并行发布**，各平台有
    独立的重试逻辑。一个平台的失败不影响其他平台。

    处理流程：
    1. 从state.requested_platforms确定目标平台
    2. 为每个平台获取相应内容（hexo_document / wechat_draft）
    3. 独立执行每个发布器（此处为顺序执行，但设计上支持异步）
    4. 将所有结果收集到publish_results列表中

    注意：虽然asyncio.gather是实现真正并行的理想方式，
    但LangGraph节点是同步运行的。发布器按顺序执行，
    但仍保持完全独立（try/except隔离）。

    节点签名遵循C1约定：(state: AgentState) -> dict

    参数：
        state: 包含hexo_document、wechat_draft、
               requested_platforms和品牌配置的代理状态。

    返回：
        包含{publish_results}的部分状态更新。
    """
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
        # Cleanup wechat process if it was started
        try:
            from src.publishers.wechat import WeChatPublisher
            # Note: We can't easily access the instance here; cleanup is handled
            # by the publisher's lifecycle management
        except ImportError:
            pass

        trace_logger.node_exit("publish", trace_id)
