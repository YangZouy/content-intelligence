"""SQLite-backed article identity, assets and publication ledger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


DEFAULT_LEDGER_PATH = Path("checkpoints/publication_ledger.sqlite")


def normalize_source_path(file_path: str) -> str:
    return str(Path(file_path).expanduser().resolve()).replace("\\", "/").casefold()


def build_article_id(file_path: str, slug: str = "") -> str:
    identity = f"slug:{slug.strip().casefold()}" if slug.strip() else f"path:{normalize_source_path(file_path)}"
    return "article_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def build_content_version(
    *,
    title: str,
    summary: str,
    tags: Iterable[str],
    body: str,
    image_urls: Iterable[str],
) -> str:
    payload = {
        "title": title.strip(),
        "summary": summary.strip(),
        "tags": sorted({tag.strip() for tag in tags if tag.strip()}),
        "body": body.replace("\r\n", "\n").strip(),
        "image_urls": sorted({url.strip() for url in image_urls if url.strip()}),
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class PublicationLedger:
    def __init__(self, path: Path = DEFAULT_LEDGER_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._setup()

    def _setup(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS articles (
                article_id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                cover_url TEXT,
                publication_date TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS publications (
                article_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                content_version TEXT NOT NULL,
                external_id TEXT,
                published_at TEXT NOT NULL,
                status TEXT NOT NULL,
                PRIMARY KEY (article_id, platform, content_version)
            );
            """
        )
        columns = {
            row[1] for row in self._connection.execute("PRAGMA table_info(articles)").fetchall()
        }
        if "publication_date" not in columns:
            self._connection.execute("ALTER TABLE articles ADD COLUMN publication_date TEXT")
        self._connection.commit()

    def ensure_article(self, article_id: str, source_path: str) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        self._connection.execute(
            """
            INSERT INTO articles(article_id, source_path, publication_date, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(article_id) DO UPDATE SET source_path=excluded.source_path, updated_at=excluded.updated_at
            """,
            (article_id, source_path, date.today().isoformat(), now, now),
        )
        self._connection.commit()
        row = self._connection.execute(
            "SELECT * FROM articles WHERE article_id = ?", (article_id,)
        ).fetchone()
        return dict(row)

    def save_cover(self, article_id: str, cover_url: str) -> None:
        if not cover_url:
            return
        self._connection.execute(
            "UPDATE articles SET cover_url = ?, updated_at = ? WHERE article_id = ?",
            (cover_url, datetime.now(timezone.utc).isoformat(), article_id),
        )
        self._connection.commit()

    def successful_publication(
        self, article_id: str, platform: str, content_version: str
    ) -> Optional[Dict[str, Any]]:
        row = self._connection.execute(
            """
            SELECT * FROM publications
            WHERE article_id = ? AND platform = ? AND content_version = ? AND status = 'success'
            """,
            (article_id, platform, content_version),
        ).fetchone()
        return dict(row) if row else None

    def record_success(
        self,
        article_id: str,
        platform: str,
        content_version: str,
        external_id: Optional[str],
        publication_date: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._connection.execute(
            """
            INSERT INTO publications(article_id, platform, content_version, external_id, published_at, status)
            VALUES (?, ?, ?, ?, ?, 'success')
            ON CONFLICT(article_id, platform, content_version) DO UPDATE SET
                external_id=excluded.external_id, published_at=excluded.published_at, status='success'
            """,
            (article_id, platform, content_version, external_id, now),
        )
        self._connection.execute(
            "UPDATE articles SET updated_at = ? WHERE article_id = ?",
            (now, article_id),
        )
        self._connection.commit()


_ledger: Optional[PublicationLedger] = None


def get_publication_ledger(force_new: bool = False, path: Optional[Path] = None) -> PublicationLedger:
    global _ledger
    if _ledger is None or force_new or path is not None:
        _ledger = PublicationLedger(path or DEFAULT_LEDGER_PATH)
    return _ledger
