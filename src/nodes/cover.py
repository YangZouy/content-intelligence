"""
封面图节点 - 获取装饰性博客封面（不走 LLM）。

设计要点（与本项目「防御式执行」风格一致）：
- 封面是「展示层素材」，不占用 LLM 调用；因此从 summary_meta 移除
  了没用的 cover_image_prompt，改由本节点负责。
- 主源：Unsplash 官方 API（需 Access Key），按 tags 拿到主题相关美图，
  失败/被墙时自动回退 Picsum（零 key、国内通常可达）。
- 兜底：Picsum（`https://picsum.photos/seed/<seed>/w/h`，零 key、国内通常可达），
  解析成最终直链后返回（picsum 会 302 跳转到 fastly CDN）。
- 落盘：默认把装饰封面上传到 OSS（config.cover.upload_to_oss），复用 image_process
  的 title_abbr，因此封面落在与文章内联图**同一个文件夹**（文件名固定 cover.jpg），
  最终 cover_url 是 OSS 直链；OSS 未配置/失败时优雅回退到远程直链。
- **同文同封面（幂等缓存）**：若 state 中已存在来自本节点上次运行生成的装饰性封面
  （Unsplash / Picsum / OSS-cover.jpg），则直接复用、不再重新获取——保证同一篇文章
  重跑时封面不漂移。（首次运行或仅有内联首图时仍会正常获取装饰封面。）
- 优雅失败：任何异常都返回 {}，绝不阻断流水线；封面留空由下游决定。

节点签名遵循 C1 约定：(state: AgentState) -> dict
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional

import httpx

from src.config_loader import get_config
from src.nodes.image_process import _generate_title_abbr
from src.observability import get_trace_logger
from src.state import AgentState

_UNSPLASH_API = "https://api.unsplash.com/photos/random"
_PICSUM_TMPL = "https://picsum.photos/seed/{seed}/{w}/{h}"

# 用于判断「是否为装饰性封面」的 URL 特征模式。
# 匹配任一模式即认为该 URL 来自 Unsplash / Picsum / OSS 封面上传，
# 触发同文同封面复用逻辑（跳过重新获取）。
_DECORATIVE_URL_PATTERNS = (
    "images.unsplash.com",
    "picsum.photos",
    "fastly.picsum.photos",
)
_OSS_COVER_SUFFIX = "cover.jpg"


def _deterministic_seed(title: str, tags: list) -> str:
    """用标题+标签生成 seed。

    注意：官方 GET /photos/random 不支持已废弃的 sig 参数，
    因此该 seed 目前仅用于 Picsum 兜底源（Picsum 确认支持 seed 参数）。
    Unsplash 的「同文同封面」通过 URL 特征匹配实现幂等复用。
    """
    basis = (title or "") + "|" + "|".join(tags or [])
    return hashlib.md5(basis.encode("utf-8")).hexdigest()[:12]


def _is_decorative_cover(url: str) -> bool:
    """判断 cover_url 是否来自本节点之前生成的装饰性封面。

    匹配条件：
    - URL 包含 Unsplash / Picsum CDN 域名，或
    - OSS 路径以 cover.jpg 结尾

    满足任一即认为「已有装饰封面」，触发同文同封面复用。
    内联首图（如 .../20260714143551.png）不会匹配此规则。
    """
    if not url:
        return False
    if url.endswith(_OSS_COVER_SUFFIX):
        return True
    return any(p in url for p in _DECORATIVE_URL_PATTERNS)


def _fetch_unsplash(
    query: str, seed: str, access_key: str, w: int, h: int
) -> Optional[str]:
    """从 Unsplash 官方 API 取一张主题相关封面。

    返回 images.unsplash.com 的**直链**（来自 API 响应的 urls.regular，
    该地址经验证可直接下载，200 OK），微信/Hexo 友好。
    任何失败（无 key / 网络 / 解析）都返回 None，交由兜底源处理。
    """
    if not access_key:
        return None
    try:
        resp = httpx.get(
            _UNSPLASH_API,
            params={
                "query": query,
                "count": 1,
                "orientation": "landscape",
            },
            headers={"Authorization": f"Client-ID {access_key}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        # 直接使用 API 返回的官方 CDN 直链（urls.regular / urls.raw），
        # 不要自己用 photo_id 拼 `photo-{id}` —— 那种拼法会得到 404。
        photo = data[0]
        url = (photo.get("urls") or {}).get("regular") or (photo.get("urls") or {}).get(
            "raw"
        )
        if not url:
            return None
        return url
    except Exception:
        return None


def _fetch_picsum(seed: str, w: int, h: int) -> Optional[str]:
    """兜底源：Picsum 随机美图（零 key、国内通常可达）。

    picsum 会 302 跳转到 fastly CDN，这里跟随重定向并返回**最终直链**，
    避免下游（尤其是微信草稿）不支持重定向。
    """
    try:
        with httpx.Client(follow_redirects=True, timeout=15) as client:
            r = client.get(_PICSUM_TMPL.format(seed=seed, w=w, h=h))
            r.raise_for_status()
            return str(r.url)
    except Exception:
        return None


def _upload_cover_to_oss(
    remote_url: str, title_abbr: str, trace_id: str
) -> Optional[str]:
    """把装饰封面上传到 OSS，使其落在与文章内联图「同一个文件夹」。

    文件夹由 `title_abbr` 决定（generate_object_key 内部用 pypinyin 转目录），
    因此只要传入与 image_process 相同的 title_abbr，封面就与正文图片同桶同目录；
    文件名固定为 cover.jpg，便于识别且确定性（重跑不漂移）。

    返回 OSS 公开直链；若 OSS 未配置 / 下载或上传失败，返回 None（由调用方回退远程直链）。
    """
    try:
        from src.oss_client import get_oss_client

        oss_client = get_oss_client()
        # 与文章图片共用同一个 title_abbr -> 同一个 pinyin 目录 -> 同一个文件夹
        object_key = oss_client.generate_object_key(title_abbr, "cover.jpg")
        oss_url = oss_client.upload_from_url(remote_url, object_key)
        return oss_url
    except Exception as e:
        log = __import__("loguru").logger
        log.bind(trace_id=trace_id).warning(
            f"Cover OSS upload failed, falling back to remote URL: {e}"
        )
        return None


def cover_image_node(state: AgentState) -> Dict[str, Any]:
    """获取装饰性博客封面，写入 state.cover_url。

    运行顺序：在 image_process 之后，因此本节点对 cover_url 拥有最终决定权
    （即使 image_process 选了首图，只要 cover.always=true 就会被覆盖）。

    **同文同封面（幂等缓存）**：若 state 中已存在装饰性封面（Unsplash/Picsum/OSS-cover），
    则直接复用、跳过重新获取。仅当无装饰封面时才执行获取逻辑。

    节点签名遵循 C1 约定：(state: AgentState) -> dict

    参数：
        state: 包含 title / tags / cover_url（来自 image_process）的代理状态。

    返回：
        部分状态更新：{"cover_url": <最终封面直链>}；
        若已有装饰性封面（复用）或禁用则返回 {}（保留现有值）。
        全源失败时也返回 {}（保留 image_process 的选择，不静默清空）。
    """
    # 取得全局共享的traceLogger实例，用于记录每个节点的执行开始
    # 结束和耗时
    trace_logger = get_trace_logger()
    trace_id = state.get("run_log", {}).get("trace_id", "")
    trace_logger.node_enter("cover_image", trace_id)

    log = __import__("loguru").logger

    try:
        config = get_config()
        cover_cfg = config.cover

        # 禁用时保留 image_process 选出的封面（首图或空）
        if not cover_cfg.get("enabled", True):
            return {}

        title = state.get("title", "") or ""
        tags = state.get("tags", []) or []
        inline_cover = state.get("cover_url", "")

        # ── 同文同封面：已有装饰性封面 → 直接复用，不重新获取 ──
        if _is_decorative_cover(inline_cover):
            log.bind(trace_id=trace_id).info(
                f"Decorative cover already exists, reusing (same-article-same-cover): "
                f"{inline_cover[:70]}"
            )
            return {}

        # 非 always 模式：已有内联封面就保留，不额外取装饰图
        if not cover_cfg.get("always", True) and inline_cover:
            return {}

        w = int(cover_cfg.get("width", 1200))
        h = int(cover_cfg.get("height", 630))
        seed = _deterministic_seed(title, tags)
        query = " ".join(tags[:3]) if tags else (title or "technology")

        access_key = cover_cfg.get("unsplash_access_key", "")

        cover_url = ""
        cover_source = "none"
        if access_key:
            u = _fetch_unsplash(query, seed, access_key, w, h)
            if u:
                cover_url = u
                cover_source = "unsplash"
        if not cover_url:
            u = _fetch_picsum(seed, w, h)
            if u:
                cover_url = u
                cover_source = "picsum"

        # 把装饰封面上传到 OSS，与文章内联图同文件夹（默认开启，可配置关闭）
        if cover_url and cover_cfg.get("upload_to_oss", True):
            title_abbr = _generate_title_abbr(state)
            oss_url = _upload_cover_to_oss(cover_url, title_abbr, trace_id)
            if oss_url:
                log.bind(trace_id=trace_id).info(
                    f"Cover uploaded to OSS (same folder as article images): "
                    f"{oss_url}"
                )
                cover_url = oss_url
                cover_source = f"oss<-{cover_source}"
            # oss_url 为 None 时保留远程直链（已在 _upload_cover_to_oss 内告警）

        if cover_url:
            log.bind(trace_id=trace_id).info(
                f"Cover image resolved: {cover_url[:70]}... (source={cover_source})"
            )
        else:
            log.bind(trace_id=trace_id).warning(
                "Cover image fetch failed from all sources; preserving existing cover"
            )

        # 有新封面才返回更新；全失败返回 {} 保留 image_process 的选择
        return {"cover_url": cover_url} if cover_url else {}

    except Exception as e:
        log.bind(trace_id=trace_id).error(f"cover_image_node error: {e}")
        # 优雅失败：绝不阻断流水线；返回 {} 保留已有值而非清空
        return {}
    finally:
        trace_logger.node_exit("cover_image", trace_id)
