from __future__ import annotations

from pathlib import Path
import shutil
import uuid

import src.nodes.publish as publish_module
from src.nodes.publish import build_idempotency_key, publish_platform
from src.publishers.github_pages import GitHubPagesPublisher


def test_idempotency_key_is_stable_and_platform_scoped():
    first = build_idempotency_key("blog", "article_1", "version_1")
    second = build_idempotency_key("blog", "article_1", "version_1")
    changed = build_idempotency_key("blog", "article_1", "version_2")
    wechat = build_idempotency_key("wechat", "article_1", "version_1")

    assert first == second
    assert first != changed
    assert first != wechat


def test_successful_platform_result_is_reused_without_publisher(monkeypatch):
    content = "blog document"
    key = build_idempotency_key("blog", "article_1", "version_1")

    def unexpected_publisher(platform):
        raise AssertionError("successful platform must not be published again")

    monkeypatch.setattr(publish_module, "_get_publisher_for_platform", unexpected_publisher)
    result = publish_platform({
        "requested_platforms": ["blog", "wechat"],
        "hexo_document": content,
        "article_id": "article_1",
        "content_version": "version_1",
        "publish_results": [{
            "platform": "blog",
            "success": True,
            "url": "https://example.com/blog",
            "attempt": 1,
            "error": None,
            "idempotency_key": key,
            "skipped": False,
        }],
    }, "blog")

    assert result["publish_results"][0]["success"] is True
    assert result["publish_results"][0]["skipped"] is True


def test_failed_platform_is_retried_and_replaced(monkeypatch):
    class Publisher:
        platform = "wechat"

        def validate(self, content, brand):
            return True, []

        def publish(self, content, brand):
            return {
                "platform": "wechat",
                "success": True,
                "url": "media_id:new-media-id",
                "attempt": 1,
                "error": None,
            }

    monkeypatch.setattr(
        publish_module,
        "_get_publisher_for_platform",
        lambda platform: Publisher(),
    )
    result = publish_platform({
        "requested_platforms": ["blog", "wechat"],
        "wechat_draft": "wechat document",
        "publish_results": [
            {
                "platform": "blog",
                "success": True,
                "url": "https://example.com/blog",
                "idempotency_key": build_idempotency_key("blog", "article_1", "blog_version"),
            },
            {
                "platform": "wechat",
                "success": False,
                "error": "temporary failure",
                "idempotency_key": build_idempotency_key("wechat", "article_1", "wechat_version"),
            },
        ],
        "article_id": "article_1",
        "content_version": "wechat_version",
        "publication_date": "2026-08-12",
    }, "wechat")

    assert len(result["publish_results"]) == 2
    assert result["publish_results"][0]["platform"] == "blog"
    assert result["publish_results"][1]["success"] is True
    assert result["publish_results"][1]["url"] == "media_id:new-media-id"


def test_blog_same_content_skips_git_side_effects(monkeypatch):
    tmp_path = Path("tests/.publish-tmp") / uuid.uuid4().hex
    tmp_path.mkdir(parents=True)
    publisher = object.__new__(GitHubPagesPublisher)
    publisher._repo_owner = "owner"
    publisher._repo_name = "repo"
    publisher._local_path = tmp_path
    publisher._posts_dir = "source/_posts"
    publisher._commit_prefix = "publish:"
    content = "---\ntitle: Existing\n---\n\nBody"
    post = tmp_path / "source/_posts/Existing.md"
    post.parent.mkdir(parents=True)
    post.write_text(content, encoding="utf-8")

    monkeypatch.setattr(publisher, "_ensure_repo", lambda: object())
    monkeypatch.setattr(
        publisher,
        "_git_commit_push",
        lambda *args: (_ for _ in ()).throw(AssertionError("git must not run")),
    )
    monkeypatch.setattr(
        publisher,
        "_build_file_url",
        lambda path: "https://example.com/existing",
    )

    try:
        result = publisher.publish(content, {})
        assert result["success"] is True
        assert result["skipped"] is True
    finally:
        shutil.rmtree(tmp_path)
