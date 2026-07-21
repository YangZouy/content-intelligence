"""
图像处理节点 - OSS上传、链接替换和封面选择。

处理摄取阶段的所有ImageRef条目：
1. 本地文件 -> 通过oss_client上传到OSS
2. 远程URL -> 下载并重新上传到OSS
3. 已有OSS URL -> 跳过（已托管）
4. 将内容中的所有图像引用替换为OSS URL
5. 选择第一张图像作为封面
6. 强制执行每篇文章最多10张图像

输出：{content_with_oss_images, cover_url, image_mapping, oss_image_count}
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List

from src.errors import OSSError
from src.oss_client import get_oss_client, AliyunOSSClient
from src.state import AgentState, ImageRef
from src.schema import SummaryMetaOutput
from src.observability import get_trace_logger


# Maximum images allowed per article (configurable)
_MAX_IMAGES_DEFAULT = 15


def _generate_title_abbr(state: AgentState) -> str:
    """生成用于OSS路径生成的短标题缩写。

    如果可用，使用LLM生成的标题，否则回退到
    file_path中的文件名。

    参数：
        state: 当前代理状态（应包含title或file_path）。

    返回：
        适合用于pypinyin路径生成的短字符串。
    """
    # Try LLM-generated title first
    title = state.get("title", "")
    if title:
        # Use first 20 chars of title for abbreviation
        return title[:20]

    # Fall back to filename
    file_path = state.get("file_path", "")
    if file_path:
        from pathlib import Path
        return Path(file_path).stem

    return "Untitled"


def _upload_single_image(
    img_ref: ImageRef,
    oss_client: AliyunOSSClient,
    title_abbr: str,
    trace_id: str,
) -> tuple[str, str]:
    """将单张图像上传到OSS并返回（原始url，oss url）。

    处理三种情况：
    - 已是OSS URL -> 原样返回
    - 本地文件路径 -> upload_local_file()
    - 远程URL -> upload_from_url()

    参数：
        img_ref: 要处理的图像引用。
        oss_client: 已初始化的OSS客户端。
        title_abbr: 用于生成对象键目录的标题缩写。
        trace_id: 用于日志记录的当前跟踪ID。

    返回：
        （原始url或路径，结果OSS公共URL）的元组。

    异常：
        OSSError: 如果上传在重试后失败。
    """
    original = img_ref["url_or_path"]
    log = __import__("loguru").logger

    # Case 1: Already an OSS URL for our bucket -> skip
    if oss_client.is_oss_url(original):
        log.bind(trace_id=trace_id).debug(f"Already OSS URL, skipping: {original[:50]}...")
        return original, original

    # Generate object key with English path (pypinyin)
    object_key = oss_client.generate_object_key(title_abbr, original)

    try:
        # Case 2: Looks like a local file path
        if (
            not original.startswith("http://")
            and not original.startswith("https://")
            and not original.startswith("data:")
        ):
            log.bind(trace_id=trace_id).debug(
                f"Uploading local file: {original} -> {object_key}"
            )
            oss_url = oss_client.upload_local_file(original, object_key)

        # Case 3: Remote URL
        else:
            log.bind(trace_id=trace_id).debug(
                f"Downloading & re-uploading URL: {original[:60]}... -> {object_key}"
            )
            oss_url = oss_client.upload_from_url(original, object_key)

        return original, oss_url

    except Exception as e:
        log.bind(trace_id=trace_id).warning(
            f"Failed to upload image '{original}': {e}"
        )
        raise OSSError(f"Image upload failed for '{original}': {e}") from e


def _replace_image_links_in_content(
    content: str,
    image_mapping: Dict[str, str],
) -> str:
    """Replace all image references in markdown content with OSS URLs.

    Matches by basename so that a local reference like
    ![](20260714143551.png) is replaced even when the
    mapping key is the resolved absolute path
    (/vault/assets/20260714143551.png). Handles both
    markdown ![alt](url) and HTML <img src="url">.

    Args:
        content: Formatted markdown content with original image references.
        image_mapping: Dict mapping original URL/path to OSS URL.

    Returns:
        Content with all image links replaced by OSS URLs.
    """
    result = content

    for original_url, oss_url in image_mapping.items():
        base = os.path.basename(original_url)

        if not base:
            # Exotic ref without a usable basename -> exact match fallback
            escaped_original = re.escape(original_url)
            result = re.sub(
                r'!\[([^\]]*)\](' + escaped_original + r'\)',
                f'![\1]({oss_url})',
                result,
            )
            result = re.sub(
                r'(<img[^>]*)src=["\']' + escaped_original + r'["\']',
                f'\1src="{oss_url}"',
                result,
                flags=re.IGNORECASE,
            )
            continue

        # Markdown ![](...base) -- match any leading path prefix
        result = re.sub(
            r'!\[([^\]]*)\]\([^)]*' + re.escape(base) + r'[^)]*\)',
            lambda m: f'![{m.group(1)}]({oss_url})',
            result,
        )
        # HTML <img src="...base" ...>
        result = re.sub(
            r'(<img[^>]*)src=["\']([^"\']*' + re.escape(base) + r'[^"\']*)["\']',
            lambda m: f'{m.group(1)}src="{oss_url}"',
            result,
            flags=re.IGNORECASE,
        )

    return result


# ---------------------------------------------------------------------------
# Main node function
# ---------------------------------------------------------------------------

def image_process_node(state: AgentState) -> Dict[str, Any]:
    """处理所有图像：上传到OSS并替换内容中的引用。

    处理流程：
    1. 从state.images获取ImageRef列表
    2. 强制执行最大图像限制（截断超出部分并发出WARNING）
    3. 对每张图像：确定类型 -> 上传或跳过
    4. 构建原始URL到OSS URL的映射
    5. 替换formatted_content中的所有图像引用
    6. 选择第一张上传的图像作为封面（可被下游cover_image节点覆盖）
    7. 返回更新后的状态

    节点签名遵循C1约定：(state: AgentState) -> dict

    参数：
        state: 包含formatted_content和images的代理状态。

    返回：
        部分状态更新，包含：
        - content_with_oss_images: 所有图像指向OSS的内容。
        - cover_url: 封面图像的OSS URL（第一张图像）。
        - image_mapping: 原始URL到OSS URL的映射字典。
        - oss_image_count: 成功上传到OSS的图像数量。
    """
    trace_logger = get_trace_logger()
    trace_id = state.get("run_log", {}).get("trace_id", "")
    trace_logger.node_enter("image_process", trace_id)

    try:
        formatted_content = state.get("formatted_content", "")
        images: List[ImageRef] = state.get("images", [])
        config = __import__("src.config_loader", fromlist=["get_config"]).get_config()

        log = __import__("loguru").logger
        max_images = config.oss.get("max_images_per_article", _MAX_IMAGES_DEFAULT)

        if not images:
            log.bind(trace_id=trace_id).info("No images found, skipping processing")
            return {
                "content_with_oss_images": formatted_content,
                "cover_url": "",
                "image_mapping": {},
                "oss_image_count": 0,
            }

        # Enforce max image limit
        if len(images) > max_images:
            log.bind(trace_id=trace_id).warning(
                f"Article has {len(images)} images, exceeding limit of {max_images}. "
                f"Truncating to first {max_images} images."
            )
            images = images[:max_images]

        # Get OSS client
        oss_client = get_oss_client()
        title_abbr = _generate_title_abbr(state)

        # Process each image
        image_mapping: Dict[str, str] = {}
        successful_uploads: List[Dict[str, str]] = []
        upload_errors: int = 0

        for i, img_ref in enumerate(images):
            try:
                original, oss_url = _upload_single_image(
                    img_ref, oss_client, title_abbr, trace_id
                )
                image_mapping[original] = oss_url
                successful_uploads.append({
                    "index": i,
                    "original": original,
                    "oss_url": oss_url,
                })
                log.bind(trace_id=trace_id).debug(
                    f"Image {i+1}/{len(images)} processed: {oss_url[-40:]}"
                )
            except OSSError as e:
                upload_errors += 1
                log.bind(trace_id=trace_id).warning(
                    f"Failed to process image {i+1}: {e}"
                )

        # Replace image links in content
        content_with_oss = _replace_image_links_in_content(
            formatted_content, image_mapping
        )

        # Select cover: first successfully uploaded image
        cover_url = ""
        if successful_uploads:
            cover_url = successful_uploads[0]["oss_url"]

        result = {
            "content_with_oss_images": content_with_oss,
            "cover_url": cover_url,
            "image_mapping": image_mapping,
            "oss_image_count": len(successful_uploads),
        }

        log.bind(trace_id=trace_id).info(
            f"Image processing complete: "
            f"{len(successful_uploads)} uploaded, "
            f"{upload_errors} failed, "
            f"cover={'set' if cover_url else 'none'}"
        )

        return result

    except OSSError:
        raise
    except Exception as e:
        log = __import__("loguru").logger
        log.bind(trace_id=trace_id).error(f"Image processing failed: {e}")
        raise OSSError(f"Image processing failed: {e}") from e
    finally:
        trace_logger.node_exit("image_process", trace_id)
