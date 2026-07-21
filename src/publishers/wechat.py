"""
WeChat Publisher - Official Account draft publishing via wenyan-mcp.

Implements Publisher protocol for WeChat Official Account (公众号) draft creation.
Communicates with wenyan-mcp (a standard MCP server) over stdio using the
MCP JSON-RPC 2.0 protocol:

    1. Launch wenyan-mcp as a subprocess (stdio transport)
    2. MCP handshake: `initialize` -> `notifications/initialized`
    3. `tools/call` with name `publish_article`
       arguments: {content: <markdown>, theme_id: <id>}
    4. Parse the returned media_id from the tool result text

wenyan-mcp requirements (see https://github.com/freeasyman/wenyan-mcp):
    - Env vars WECHAT_APP_ID / WECHAT_APP_SECRET (inherited from os.environ)
    - The article's front-matter must contain `title` and `cover`
    - The machine's public IP must be in the 公众号 IP 白名单 (errcode 40164)

Supports:
    - Launch via `npx -y @wenyan-md/mcp` or a local `wenyan-mcp` binary
    - Mock mode when no wenyan command is configured (dev/testing)
    - Robust line reads with timeout (no reliance on select, which is
      unreliable for pipes on Windows)
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from src.config_loader import get_config
from src.errors import WeChatError, retry_with_backoff
from src.publishers.base import Publisher
from src.state import BrandConfig, PublishResultItem


class WeChatPublisher(Publisher):
    """Publisher implementation for WeChat Official Account drafts.

    Uses wenyan-mcp (standard MCP server) as an intermediary to communicate
    with the WeChat API. Communication happens via MCP JSON-RPC over the
    subprocess stdio transport.

    Attributes:
        _wenyan_cmd: Resolved subprocess command (list) or None (mock mode).
        _theme_id: wenyan theme to apply (default 'default').
        _tunnel_enabled: Whether Cloudflare Tunnel proxy is active.
        _timeout_seconds: Per-request timeout in seconds.
        _handshake_timeout: Timeout for the initial MCP handshake.
        _max_retries: Max retry attempts for wechat operations.
        _process: The running wenyan-mcp subprocess.
        _request_id: Monotonic JSON-RPC request id counter.
    """

    def __init__(self) -> None:
        """Initialize publisher from configuration."""
        config = get_config()

        self._wenyan_path: str = config.wechat.get("wenyan_path", "")
        self._theme_id: str = config.wechat.get("theme_id", "default")
        self._tunnel_enabled: bool = config.wechat.get("tunnel_enabled", False)
        self._timeout_seconds: int = config.wechat.get("timeout_seconds", 60)
        self._handshake_timeout: int = config.wechat.get("handshake_timeout", 180)
        self._max_retries: int = config.wechat.get("max_retries", 2)

        # Track the running subprocess
        self._process: Optional[subprocess.Popen] = None
        self._request_id: int = 0

    @property
    def platform(self) -> str:
        """Return 'wechat' as platform identifier."""
        return "wechat"

    # -----------------------------------------------------------------
    # Command resolution & mock detection
    # -----------------------------------------------------------------

    def _build_wenyan_cmd(self) -> Optional[List[str]]:
        """Resolve the wenyan-mcp launch command.

        Supports both a plain executable path and a command with arguments
        (e.g. ``npx -y @wenyan-md/mcp``). Returns None when no usable
        command is configured, which signals MOCK mode.

        Returns:
            List of argv tokens, or None to indicate mock mode.
        """
        raw = (self._wenyan_path or "").strip()
        if not raw:
            return None

        parts = raw.split()
        # A single token that is neither an existing file nor a command on
        # PATH (e.g. the placeholder '/path/to/wenyanserver.exe') -> mock.
        if len(parts) == 1 and not os.path.exists(parts[0]) \
                and shutil.which(parts[0]) is None:
            return None
        return parts

    # -----------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------

    def validate(
        self,
        content: str,
        brand: BrandConfig,
    ) -> Tuple[bool, List[str]]:
        """Validate WeChat draft content before publishing.

        Checks:
        - Content is not empty
        - Content length within WeChat limits (≤100000 chars for text)
        - front-matter contains a `title` (wenyan requirement)

        Args:
            content: WeChat draft markdown content.
            brand: Brand configuration.

        Returns:
            Tuple of (is_valid, error_messages list).
        """
        errors: List[str] = []

        if not content or not content.strip():
            errors.append("Content is empty")
            return False, errors

        if len(content) > 100_000:
            errors.append(f"Content too long ({len(content)} chars). "
                         f"WeChat limit is ~100,000 characters.")

        if not content.lstrip().startswith("---"):
            errors.append("Missing YAML front-matter (wenyan requires title/cover)")
        elif "title:" not in content:
            errors.append("front-matter missing 'title' (required by wenyan)")

        return len(errors) == 0, errors

    # -----------------------------------------------------------------
    # Public publish entry point
    # -----------------------------------------------------------------

    @retry_with_backoff(max_attempts=3, base_delay=2.0)
    def publish(
        self,
        content: str,
        brand: BrandConfig,
    ) -> PublishResultItem:
        """Publish a draft to WeChat Official Account via wenyan-mcp.

        Workflow:
        1. Fall back to MOCK mode if no wenyan command is configured
        2. Start / reuse the wenyan-mcp subprocess and perform MCP handshake
        3. Send `tools/call` -> `publish_article`
        4. Return the media_id as the result URL

        Args:
            content: WeChat draft markdown content (with front-matter).
            brand: Brand configuration.

        Returns:
            PublishResultItem with success status and media_id as URL.

        Raises:
            WeChatError: If wenyan-mcp communication or publishing fails.
        """
        log = __import__("loguru").logger

        if self._build_wenyan_cmd() is None:
            log.warning(
                "wenyan command not configured. "
                "Using MOCK mode — no actual draft created."
            )
            return self._mock_publish(content)

        try:
            media_id = self._send_create_draft_request(content)
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

    def retry(
        self,
        prev_result: PublishResultItem,
        max_attempts: int = 3,
    ) -> PublishResultItem:
        """Retry a failed WeChat publish operation.

        Args:
            prev_result: Previous failed result.
            max_attempts: Maximum total attempts.

        Returns:
            Result after retries.
        """
        last_error = prev_result.get("error", "Unknown error")
        for attempt in range(2, max_attempts + 1):
            try:
                delay = min(2 ** (attempt - 1), 8)
                time.sleep(delay)
                raise WeChatError(
                    f"Retry attempt {attempt}: requires stored content reference",
                )
            except WeChatError as e:
                last_error = str(e)
        return PublishResultItem(
            platform="wechat",
            success=False,
            url=None,
            attempt=max_attempts,
            error=f"All {max_attempts} attempts failed. Last: {last_error}",
        )

    # -----------------------------------------------------------------
    # MCP stdio client implementation
    # -----------------------------------------------------------------

    def _start_wenyan_process(self) -> subprocess.Popen:
        """Start wenyan-mcp as a subprocess with stdio pipes + MCP handshake."""
        cmd = self._build_wenyan_cmd()
        if cmd is None:
            raise WeChatError("wenyan-mcp command is not configured")

        env = os.environ.copy()
        # Be explicit about credentials (also inherited from os.environ).
        for key in ("WECHAT_APP_ID", "WECHAT_APP_SECRET"):
            val = os.environ.get(key)
            if val:
                env[key] = val
        if self._tunnel_enabled:
            env["CLOUDFLARE_TUNNEL"] = "true"

        # Resolve the executable to an absolute path. On Windows the configured
        # command may be `npx` (resolving to npx.CMD); a bare name without an
        # extension fails to launch via CreateProcess, so use the full path.
        launch_cmd = list(cmd)
        resolved_exe = shutil.which(launch_cmd[0])
        if resolved_exe:
            launch_cmd[0] = resolved_exe

        process = subprocess.Popen(
            launch_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",  # tolerate any non-UTF-8 bytes on stdout/stderr
            env=env,
            bufsize=1,  # line buffered
        )
        self._process = process
        self._handshake()
        return process

    def _handshake(self) -> None:
        """Perform the MCP initialize handshake with wenyan-mcp."""
        # 1) initialize
        self._send_json_rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "content-dispatcher", "version": "0.1.0"},
            },
            timeout=self._handshake_timeout,
        )
        # 2) notify the server we are initialized (required by MCP SDK)
        self._send_notification("notifications/initialized", {})

    def _next_request_id(self) -> int:
        """Generate the next JSON-RPC request ID."""
        self._request_id += 1
        return self._request_id

    def _readline_timeout(self, proc: subprocess.Popen, timeout: int) -> str:
        """Read one line from proc.stdout with a wall-clock timeout.

        Uses a daemon reader thread so it works reliably on Windows (where
        select() does not support pipes).
        """
        q: "queue.Queue[Any]" = queue.Queue()

        def _reader() -> None:
            try:
                q.put(proc.stdout.readline())
            except Exception as exc:  # pragma: no cover - defensive
                q.put(exc)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise WeChatError(
                f"Timeout waiting for wenyan-mcp response ({timeout}s)"
            )
        result = q.get()
        if isinstance(result, Exception):
            raise WeChatError(f"wenyan-mcp read error: {result}")
        return result

    def _send_notification(self, method: str, params: Dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        proc = self._ensure_process()
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        proc.stdin.write(json.dumps(notification, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def _send_json_rpc(
        self,
        method: str,
        params: Dict[str, Any],
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Send a JSON-RPC request and read the matching response.

        Implements the MCP JSON-RPC 2.0 protocol over stdio. Notifications
        and unrelated messages (no matching id) are skipped.

        Args:
            method: RPC method name (e.g. 'initialize', 'tools/call').
            params: Method parameters dictionary.
            timeout: Read timeout in seconds (defaults to _timeout_seconds).

        Returns:
            Parsed JSON-RPC response dict (the one matching our request id).

        Raises:
            WeChatError: If communication fails or the server returns an error.
        """
        timeout = timeout or self._timeout_seconds
        proc = self._ensure_process()

        request_id = self._next_request_id()
        request = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": request_id,
        }
        request_str = json.dumps(request, ensure_ascii=False) + "\n"

        try:
            proc.stdin.write(request_str)
            proc.stdin.flush()

            # Read until we get the response matching our request id.
            while True:
                raw_response = self._readline_timeout(proc, timeout)
                if not raw_response:
                    stderr_output = proc.stderr.read()
                    raise WeChatError(
                        f"wenyan-mcp closed stdout unexpectedly. "
                        f"stderr: {stderr_output[:500]}"
                    )
                line = raw_response.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue  # ignore non-JSON noise

                # Skip notifications / responses for other requests.
                if "id" not in msg:
                    continue
                if msg["id"] != request_id:
                    continue

                if "error" in msg:
                    err = msg["error"]
                    raise WeChatError(
                        f"JSON-RPC error from wenyan-mcp: "
                        f"{err.get('code', 'unknown')}: "
                        f"{err.get('message', 'unknown')}"
                    )
                return msg

        except WeChatError:
            raise
        except OSError as e:
            raise WeChatError(
                f"I/O error communicating with wenyan-mcp: {e}"
            ) from e

    def _send_create_draft_request(self, content: str) -> str:
        """Call wenyan-mcp `publish_article` and return the media_id.

        Args:
            content: Markdown draft content (with front-matter).

        Returns:
            media_id string extracted from the tool result.

        Raises:
            WeChatError: If draft creation fails.
        """
        response = self._send_json_rpc(
            "tools/call",
            {
                "name": "publish_article",
                "arguments": {
                    "content": content,
                    "theme_id": self._theme_id or "default",
                },
            },
            timeout=self._timeout_seconds,
        )

        result = response.get("result", {})
        if "error" in result:
            err = result["error"]
            raise WeChatError(
                f"wenyan publish_article failed: "
                f"{err.get('message', err)}"
            )

        # Result text e.g. "Your article was successfully published to
        # '公众号草稿箱'. The media ID is XXXX."
        # On failure wenyan embeds the error in the result text instead of
        # using the MCP error channel, e.g. "执行工具失败: 40164: invalid ip ...".
        text = ""
        for item in result.get("content", []):
            if item.get("type") == "text":
                text += item.get("text", "")

        import re
        match = re.search(r"media ID is (\S+)", text)
        if not match:
            raise WeChatError(
                f"wenyan publish_article failed: {text.strip()}"
            )
        return match.group(1)

    def _ensure_process(self) -> subprocess.Popen:
        """Get or start the wenyan-mcp subprocess (with handshake)."""
        if self._process is None or self._process.poll() is not None:
            self._start_wenyan_process()
        assert self._process is not None
        return self._process

    def _mock_publish(self, content: str) -> PublishResultItem:
        """Simulate a successful publish when wenyan-mcp is unavailable.

        Allows the pipeline to run end-to-end during development and testing
        without a real wenyan-mcp server or WeChat credentials.

        Args:
            content: Draft content (validated but not actually sent).

        Returns:
            Simulated successful PublishResultItem.
        """
        log = __import__("loguru").logger
        log.info(
            "[MOCK] WeChat draft would be created here. "
            f"Content length: {len(content)} chars"
        )
        time.sleep(0.5)
        return PublishResultItem(
            platform="wechat",
            success=True,
            url="media_id:mock_draft_001",
            attempt=1,
            error=None,
        )

    def cleanup(self) -> None:
        """Terminate the wenyan-mcp subprocess if running."""
        if self._process is not None and self._process.poll() is None:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self._process.kill()
            finally:
                self._process = None
