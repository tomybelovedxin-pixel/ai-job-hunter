"""Hacker News「Who is hiring?」月度帖。

数据来自 HN 官方公开 API（Algolia 检索 + 帖子树），是社区自发的招聘帖，
结构不统一，所以只做保守抽取：每条评论的第一行当作职位抬头，其余当正文。
匹配度判断交给后面的分析环节，这里不做美化。
"""

from __future__ import annotations

import re
from typing import Any

from ..textutil import clean_text, html_to_text
from .base import Filters, Source, SourceResult, http_json, job_dict

SEARCH_API = "https://hn.algolia.com/api/v1/search"
ITEM_API = "https://hn.algolia.com/api/v1/items"
STORY_URL = "https://news.ycombinator.com/item?id="


class HackerNewsSource(Source):
    name = "hn"
    label = "Hacker News Who is hiring"
    homepage = "https://news.ycombinator.com"
    docs = "https://hn.algolia.com/api"
    notes = "社区帖，文本非结构化；以英文岗位为主，第一行通常是 公司 | 岗位 | 地点"

    def fetch(self, filters: Filters) -> SourceResult:
        story = self._latest_story()
        if not story:
            return SourceResult(source=self.name, note="没找到最近的招聘月帖")
        story_id = story.get("objectID")
        thread = http_json(f"{ITEM_API}/{story_id}")
        children = thread.get("children") or []
        jobs: list[dict[str, Any]] = []
        for child in children:
            text = html_to_text(child.get("text") or "")
            if not text:
                continue
            head, _, rest = text.partition("\n")
            head = clean_text(head)
            if not filters.match(f"{head} {rest[:400]}"):
                continue
            company, title = self._split_head(head)
            comment_id = str(child.get("id"))
            jobs.append(
                job_dict(
                    platform=self.name,
                    external_id=comment_id,
                    title=title or head[:80],
                    company=company,
                    url=f"{STORY_URL}{comment_id}",
                    description=clean_text(f"{head}\n{rest}"),
                    city="远程/海外",
                    tags=["HN 招聘帖"],
                    extra={
                        "story_id": story_id,
                        "story_title": story.get("title"),
                        "story_url": f"{STORY_URL}{story_id}",
                        "author": child.get("author"),
                        "created_at": child.get("created_at"),
                    },
                )
            )
            if len(jobs) >= filters.limit:
                break
        return SourceResult(
            source=self.name,
            jobs=jobs,
            raw_count=len(children),
            note=f"取自《{story.get('title')}》，共 {len(children)} 条顶层评论",
        )

    @staticmethod
    def _latest_story() -> dict[str, Any] | None:
        data = http_json(
            SEARCH_API,
            params={
                "query": '"Ask HN: Who is hiring?"',
                "tags": "story,author_whoishiring",
                "hitsPerPage": 5,
            },
        )
        hits = (data or {}).get("hits") or []
        for hit in hits:
            title = (hit.get("title") or "").lower()
            if "who is hiring" in title:
                return hit
        return hits[0] if hits else None

    @staticmethod
    def _split_head(head: str) -> tuple[str, str]:
        parts = [p.strip() for p in re.split(r"\s*[|｜]\s*", head) if p.strip()]
        if not parts:
            return "", ""
        company = parts[0]
        title = parts[1] if len(parts) > 1 else ""
        return company[:80], title[:120]
