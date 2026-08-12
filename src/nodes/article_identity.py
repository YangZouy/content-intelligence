"""Resolve stable article identity and persisted article-level metadata."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict

from src.publication_ledger import build_article_id, get_publication_ledger, normalize_source_path
from src.state import AgentState


def article_identity_node(state: AgentState) -> Dict[str, Any]:
    file_path = state.get("file_path", "")
    if not file_path:
        raise ValueError("file_path is required to build article identity")
    article_id = build_article_id(file_path, state.get("article_slug", ""))
    article = get_publication_ledger().ensure_article(article_id, normalize_source_path(file_path))
    return {
        "article_id": article_id,
        "publication_date": article.get("publication_date") or date.today().isoformat(),
        "persisted_cover_url": article.get("cover_url") or "",
    }
