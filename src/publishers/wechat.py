from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from src.config_loader import get_config
from src.errors import WeChatError
from src.publishers.base import Publisher
from src.state import BrandConfig, PublishResultItem


class WeChatPublisher(Publisher):
    def __init__(self) -> None:
        config = get_config()
        self._server_url: str = config.wechat.get("server_url", "http://localhost:3000").rstrip("/")
        self._api_key: Optional[str] = config.wechat.get("api_key") or None
        self._theme_id: str = config.wechat.get("theme_id", "default")
        self._cli_command: str = config.wechat.get("cli_command", "wenyan")
        self._timeout_seconds: int = config.wechat.get("timeout_seconds", 60)

    @property
    def platform(self) -> str:
        return "wechat"

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
        except Exception as exc:
            log.error(f"WeChat publish failed: {exc}")
            raise WeChatError(f"WeChat draft creation failed: {exc}") from exc

    def _create_draft(self, content: str) -> str:
        """写在系统临时区域中，用完即删"""
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as file:
            file.write(content)
            temp_path = Path(file.name)

        try:
            cli_executable = shutil.which(self._cli_command) or self._cli_command
            command = [
                cli_executable,
                "publish",
                "--file",
                str(temp_path),
                "--server",
                self._server_url,
                "--theme",
                self._theme_id,
            ]
            if self._api_key:
                command.extend(["--api-key", self._api_key])

            # 剥离代理环境变量：wenyan CLI 会自动读取 HTTP(S)_PROXY 并走本地代理，
            # 而本地代理转发「裸 IP:端口」请求经常不稳定（fetch failed）；
            # 服务器 IP 国内直连可达，无需代理。
            env = {
                key: value
                for key, value in os.environ.items()
                if key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
            }
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout_seconds,
                check=False,
                env=env,
            )
            output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
            # 成功判定以 Media ID 为准，而非退出码：
            # Windows 下 npm 的 wenyan.cmd 包装脚本（goto #_undefined_# 技巧）在部分
            # subprocess 环境会把退出码污染成 1，但 Media ID 只有服务端真正创建
            # 草稿成功后才会输出，是更可靠的成功信号。
            media_id_match = re.search(r"Media ID:\s*(\S+)", output)
            if media_id_match:
                return media_id_match.group(1)
            if result.returncode != 0:
                raise WeChatError(f"Wenyan CLI publish failed (exit {result.returncode}): {output}")
            raise WeChatError(f"Wenyan CLI did not return a media ID: {output}")
        except FileNotFoundError as exc:
            raise WeChatError(
                f"Wenyan CLI executable not found: {self._cli_command}. "
                "Install it with: npm install -g @wenyan-md/cli"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise WeChatError(f"Wenyan CLI publish timed out after {self._timeout_seconds}s") from exc
        finally:
            temp_path.unlink(missing_ok=True)
