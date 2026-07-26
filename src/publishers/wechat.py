from __future__ import annotations

import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

import httpx

from src.config_loader import get_config
from src.errors import WeChatError
from src.publishers.base import Publisher
from src.state import BrandConfig, PublishResultItem


class WeChatPublisher(Publisher):
    def __init__(self) -> None:
        config = get_config()
        self._server_url: str = config.wechat.get("server_url", "http://localhost:3000").rstrip("/")
        self._api_key: Optional[str] = config.wechat.get("api_key") or None
        # 文章主题样式
        self._theme_id: str = config.wechat.get("theme_id", "default")
        # HTTP超时请求
        self._timeout_seconds: int = config.wechat.get("timeout_seconds", 60)

    @property
    def platform(self) -> str:
        return "wechat"

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["x-api-key"] = self._api_key
        return h

    # 内容验证
    def validate(self, content: str, brand: BrandConfig) -> Tuple[bool, List[str]]:
        errors: List[str] = []
        if not content or not content.strip():
            errors.append("Content is empty")
            return False, errors
        if len(content) > 100_000:
            errors.append(f"Content too long ({len(content)} chars). WeChat limit is ~100,000 characters.")
        if not content.lstrip().startswith("---"):
            errors.append("Missing YAML front-matter (wenyan requires title/cover)")
        elif "title:" not in content:
            errors.append("front-matter missing 'title' (required by wenyan)")
        return len(errors) == 0, errors

    def publish(self, content: str, brand: BrandConfig) -> PublishResultItem:
        log = __import__("loguru").logger
        try:
            media_id = self._create_draft(content)
            return PublishResultItem(
                platform="wechat",
                success=True,
                url=f"media_id:{media_id}",
                attempt=1,
                error=None,
            )
        except WeChatError:
            raise
        except Exception as e:
            log.error(f"WeChat publish failed: {e}")
            raise WeChatError(f"WeChat draft creation failed: {e}") from e

    # 创建草稿 + 返回媒体id
    def _create_draft(self, content: str) -> str:
        """Upload markdown to wenyan server and trigger publish, return media_id."""
        file_id = self._upload(content)
        return self._publish(file_id)

    # 文件上传
    def _upload(self, content: str) -> str:
        # 临时文件创建，delete表示不自动删除
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
            f.write(content)
            # 获取临时文件路径
            tmp_path = Path(f.name)

        try:
            # 通过http上传文件
            with httpx.Client(timeout=self._timeout_seconds) as client:
                with open(tmp_path, "rb") as fh:
                    resp = client.post(
                        # 上传接口
                        f"{self._server_url}/upload",
                        # 文件上传的content-type为multipart/form-data，省略的话httpx会自动设置
                        headers={k: v for k, v in self._headers().items() if k != "Content-Type"},
                        files={"file": ("article.md", fh, "text/markdown")},
                    )
            # 处理响应 httpx内置方法 检查HTTP响应状态码
            resp.raise_for_status()
            # 解析json
            data = resp.json()
            if not data.get("success"):
                raise WeChatError(f"Upload failed: {data}")
            return data["data"]["fileId"]
        finally:
            # 资源清理
            tmp_path.unlink(missing_ok=True)

    # 触发发布流程
    def _publish(self, file_id: str) -> str:
        # 创建HTTP客户端
        with httpx.Client(timeout=self._timeout_seconds) as client:
            resp = client.post(
                f"{self._server_url}/publish",
                headers=self._headers(),
                json={"fileId": file_id, "theme": self._theme_id},
            )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            raise WeChatError(f"Publish failed: {data}")

        draft_id = (data.get("data") or {}).get("draft_id") or (data.get("data") or {}).get("media_id", "")
        return draft_id
