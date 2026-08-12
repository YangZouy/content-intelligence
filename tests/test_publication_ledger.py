from __future__ import annotations

from pathlib import Path

from src.publication_ledger import (
    PublicationLedger,
    build_article_id,
    build_content_version,
)


def test_article_id_uses_source_path_but_slug_survives_file_move():
    first_path = build_article_id("notes/article.md")
    same_path = build_article_id("notes/./article.md")
    moved_path = build_article_id("archive/article.md")
    slug_before = build_article_id("notes/article.md", "reliable-workflow")
    slug_after = build_article_id("archive/article.md", "reliable-workflow")

    assert first_path == same_path
    assert first_path != moved_path
    assert slug_before == slug_after


def test_content_version_excludes_date_cover_run_and_temp_paths():
    common = {
        "title": "Article",
        "summary": "Summary",
        "tags": ["workflow", "AI"],
        "body": "# Article\n\nBody\n\n![diagram](https://oss.example.com/diagram.png)",
        "image_urls": ["https://oss.example.com/diagram.png"],
    }
    first = build_content_version(**common)
    second = build_content_version(**common)

    assert first == second


def test_content_version_changes_for_semantic_article_changes():
    base = build_content_version(
        title="Article",
        summary="Summary",
        tags=["one", "two"],
        body="Body",
        image_urls=[],
    )
    changed_body = build_content_version(
        title="Article",
        summary="Summary",
        tags=["one", "two"],
        body="Changed body",
        image_urls=[],
    )
    assert base != changed_body


def test_first_publication_date_and_cover_remain_stable():
    ledger = PublicationLedger(Path(":memory:"))
    article_id = "article_test"
    ledger.ensure_article(article_id, "c:/notes/article.md")
    ledger.save_cover(article_id, "https://oss.example.com/cover.jpg")
    ledger.record_success(
        article_id,
        "blog",
        "version_1",
        "https://github.com/example/article",
        "2026-08-12",
    )
    ledger.record_success(
        article_id,
        "wechat",
        "version_2",
        "media_id:abc",
        "2026-09-01",
    )

    article = ledger.ensure_article(article_id, "c:/notes/article.md")
    assert article["cover_url"] == "https://oss.example.com/cover.jpg"
    assert article["publication_date"] is not None


def test_successful_publication_is_found_across_new_workflow_runs():
    ledger = PublicationLedger(Path(":memory:"))
    ledger.ensure_article("article_test", "c:/notes/article.md")
    ledger.record_success(
        "article_test",
        "wechat",
        "version_1",
        "media_id:abc",
        "2026-08-12",
    )

    record = ledger.successful_publication("article_test", "wechat", "version_1")
    assert record is not None
    assert record["external_id"] == "media_id:abc"
