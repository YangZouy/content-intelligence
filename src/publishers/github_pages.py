"""
GitHub Pages发布器 - 通过Git发布Hexo博客文章。

为基于GitHub的Hexo博客部署实现Publisher协议。

流程：
1. 克隆或打开本地Hexo仓库
2. 将文章写入source/_posts/<title>.md，包含front-matter
3. Git add, commit, push

支持：
- 增量更新（文件存在 -> 覆盖）
- 可配置的仓库路径、提交消息前缀
- 通过GITHUB_TOKEN进行基于令牌的身份认证
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.config_loader import get_config
from src.errors import (
    GitHubPublishError,
    retry_with_backoff,
)
from src.publishers.base import Publisher
from src.state import BrandConfig, PublishResultItem
from src.observability import get_trace_logger


class GitHubPagesPublisher(Publisher):
    """用于GitHub Pages / Hexo博客的发布器实现。

    使用GitPython进行Git操作。管理配置的Hexo仓库的本地克隆，
    并将文章写入source/_posts/。

    属性：
        _repo_owner: GitHub仓库所有者。
        _repo_name: 仓库名称。
        _token: GitHub个人访问令牌。
        _local_path: 仓库克隆/拉取的本地目录。
        _posts_dir: 仓库内的文章目录。
        _commit_prefix: 自动生成的提交消息的前缀字符串。
    """

    def __init__(self) -> None:
        """Initialize publisher from configuration values."""
        config = get_config()

        # Parse hexo_repo into owner/repo parts
        hexo_repo = config.github.get("hexo_repo", "")
        if "/" in hexo_repo:
            parts = hexo_repo.split("/")
            self._repo_owner = parts[0]
            self._repo_name = parts[1] if len(parts) > 1 else ""
        else:
            self._repo_owner = config.github.get("username", "")
            self._repo_name = hexo_repo

        self._token = config.github.get("token", "")
        self._username = config.github.get("username", "")
        self._local_path: Path = Path(
            config.github.get("local_repo_path", "temp/hexo_repo")
        ).resolve()
        self._posts_dir: str = config.github.get("posts_dir", "source/_posts")
        self._commit_prefix: str = config.github.get("commit_prefix", "publish:")

    @property
    def platform(self) -> str:
        return "blog"

    def validate(
        self,
        content: str,
        brand: BrandConfig,
    ) -> Tuple[bool, List[str]]:
        """发布前验证Hexo文档。

        检查：
        - 内容非空
        - 包含YAML front-matter分隔符（---）
        - front-matter中至少有标题
        """
        errors: List[str] = []

        if not content or not content.strip():
            errors.append("Content is empty")
            return False, errors

        if "---" not in content:
            errors.append("Missing YAML front-matter delimiters (---)")
            return False, errors

        # Check for title in front-matter
        title_match = re.search(r'^title:\s*(.+)$', content, re.MULTILINE)
        if not title_match:
            errors.append("Missing 'title' field in front-matter")

        return len(errors) == 0, errors

    # 函数抛出异常，装饰器自动重试，延迟2s，重试3次
    @retry_with_backoff(max_attempts=3, base_delay=2.0)
    def publish(
        self,
        content: str,
        brand: BrandConfig,
    ) -> PublishResultItem:
        """将Hexo博客文章发布到GitHub Pages。

        完整工作流程：
        1. 确保本地仓库存在（如需要则克隆，如已存在则拉取）
        2. 从front-matter中提取标题作为文件名
        3. 将文件写入source/_posts/<清理后的标题>.md
        4. Git add + commit + push
        """
        trace_id = get_trace_logger().generate_trace_id()  # fallback
        log = __import__("loguru").logger.bind(trace_id=trace_id)

        try:
            # Step 1: Ensure repository is ready
            repo = self._ensure_repo()

            # Step 2: Extract title for filename
            title = self._extract_title(content)
            filename = self._sanitize_filename(title) + ".md"
            posts_path = self._local_path / self._posts_dir
            posts_path.mkdir(parents=True, exist_ok=True)
            filepath = posts_path / filename

            # Step 3: Write file
            is_update = filepath.exists()
            filepath.write_text(content, encoding="utf-8")

            action = "updated" if is_update else "created"
            log.debug(f"GitHub: {action} {filepath}")

            # Step 4: Git operations
            commit_msg = f"{self._commit_prefix} {title}"
            result_url = self._git_commit_push(repo, str(filepath), commit_msg)

            return PublishResultItem(
                platform="blog",
                success=True,
                url=result_url,
                attempt=1,
                error=None,
            )

        except Exception as e:
            log.error(f"GitHub publish failed: {e}")
            raise GitHubPublishError(
                f"GitHub Pages publish failed: {e}",
                platform="blog",
                attempt=1,
            ) from e

    # 保证本地仓库有一个可用的、最新的Git仓库
    def _ensure_repo(self) -> Any:
        try:
            from git import Repo as GitRepo
        except ImportError:
            raise GitHubPublishError(
                "GitPython is required but not installed. "
                "Run: pip install GitPython"
            )

        # 将remote URL刷新成带token的URL（防止旧仓库残留过期token导致push失败）
        remote_url = (
            f"https://{self._token}@github.com/"
            f"{self._repo_owner}/{self._repo_name}.git"
        )

        if self._local_path.exists():
            try:
                # 打开本地仓库
                repo = GitRepo(self._local_path)
                # 获取远程仓库
                origin = repo.remotes.origin
                try:
                    # 更新远程仓库（仓库迁移、http协议更换成SSH、token刷新等导致的url更换）
                    origin.set_url(remote_url)
                except Exception:
                    pass
                # rebase拉取最新仓库 （保持提交历史线性，避免不必要的merge commit）
                origin.pull(rebase=True)
                return repo
            except Exception:
                # 任何失败（网络/冲突/仓库损坏 rmtree删除，强制重试clone）
                import shutil
                shutil.rmtree(self._local_path, ignore_errors=True)

        # 本地不存在直接clone一个新仓库
        self._local_path.parent.mkdir(parents=True, exist_ok=True)
        repo = GitRepo.clone_from(remote_url, str(self._local_path))
        return repo

    def _extract_title(self, content: str) -> str:
        match = re.search(r'^title:\s*(.+)$', content, re.MULTILINE)
        if match:
            return match.group(1).strip().strip("'\"")
        return "Untitled"

    # 将任务标题转换为安全的文件名
    # 在任何系统都合法
    @staticmethod
    def _sanitize_filename(title: str) -> str:
        """Convert title to a safe filename (ASCII, no special chars)."""
        # 替换特殊字符为-
        safe = re.sub(r'[\\/:*?"<>|\n\r\t]', '-', title)
        # 合并多个连字符----
        safe = re.sub(r'-{2,}', '-', safe)
        # 清除首尾字符
        safe = safe.strip('- ')
        # 截断
        if len(safe) > 100:
            safe = safe[:100]
        return safe or "Untitled"

    def _git_commit_push(
        self,
        repo: Any,
        filepath: str,
        commit_msg: str,
    ) -> Optional[str]:
        
        repo_root = Path(repo.working_tree_dir).resolve()
        abs_file = Path(filepath).resolve()
        if repo_root in abs_file.parents or abs_file == repo_root:
            stage_path = str(abs_file.relative_to(repo_root))
        else:
            stage_path = str(abs_file)

        # 清理残留所文件：上次Git操作异常中断
        lock_path = Path(repo.git_dir) / "index.lock"
        if lock_path.exists():
            try:
                lock_path.unlink()
            except OSError:
                pass

        # 暂存特定文件
        repo.index.add([stage_path])

        # 检查是否有变化
        if not repo.index.diff("HEAD"):
            pass

        # 提交
        repo.index.commit(commit_msg)

        # 推送
        origin = repo.remotes.origin
        push_info = origin.push()[0]

        if push_info.flags & push_info.ERROR:
            raise GitHubPushFailedError(
                f"Git push failed: {push_info.summary}"
            )

        # 构建访问链接
        rel_path = Path(stage_path).as_posix()
        default_branch = repo.active_branch.name
        url = (
            f"https://github.com/{self._repo_owner}/{self._repo_name}/blob/"
            f"{default_branch}/{rel_path}"
        )
        return url


class GitHubPushFailedError(GitHubPublishError):
    pass
