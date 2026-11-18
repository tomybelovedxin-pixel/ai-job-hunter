"""电鸭社区（eleduck.com）：国内远程 / 兼职 / 外包岗位最集中的社区。

用的是官网自己的公开接口：列表 `svc.eleduck.com/api/v1/posts`，详情按 id 取。
不需要登录、不绕任何限制。分类 5 是"社区帖子招聘"，帖子自带标签组：
招聘类型（全职/兼职/外包零活）、工作方式（线上兼职/全职远程/坐班）、城市、领域。
这些标签正好用来判断"能不能异地做"，所以不做猜测式解析。
"""

from __future__ import annotations

import re
import time
from typing import Any

from ..textutil import clean_text, one_line
from .base import (Filters, Source, SourceError, SourceResult, city_allowed,
                   http_json, job_dict)

LIST_API = "https://svc.eleduck.com/api/v1/posts"
DETAIL_API = "https://svc.eleduck.com/api/v1/posts/{post_id}"
POST_URL = "https://eleduck.com/posts/{post_id}"

JOB_CATEGORY = 5  # 社区帖子招聘

# 标题里常见写法：15-18k、6000-9000 元/月、200 元/天
SALARY_RE = re.compile(
    r"(\d+(?:\.\d+)?\s*[-~至]\s*\d+(?:\.\d+)?\s*[kK万])|(\d+(?:\.\d+)?\s*[kK]\s*[-~至]\s*\d+(?:\.\d+)?\s*[kK]?)"
    r"|(\d+\s*[-~至]\s*\d+\s*元\s*/?\s*(?:天|日|月|小时))|(月薪\s*\d+)"
)

# 工作方式标签里，这些表示可以异地做
REMOTE_TAGS = ("线上兼职", "全职远程", "远程", "兼职", "外包零活", "项目制", "按单结算")


class EleduckSource(Source):
    name = "eleduck"
    label = "电鸭社区（远程/兼职/外包）"
    homepage = "https://eleduck.com"
    docs = LIST_API
    notes = ("官网公开接口，无需登录；以个人/小团队发的远程兼职外包为主；"
             "需要自己判断对方是否靠谱，诈骗类帖子会被标记")
    city_scoped = False

    def fetch(self, filters: Filters) -> SourceResult:
        max_pages = max(1, min(3, filters.pages))
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        raw_count = 0

        for page in range(1, max_pages + 1):
            payload = http_json(LIST_API, params={"page": page, "category": JOB_CATEGORY})
            posts = (payload or {}).get("posts") or []
            if not posts:
                break
            raw_count += len(posts)
            for post in posts:
                post_id = str(post.get("id") or "")
                if not post_id or post_id in seen:
                    continue
                if post.get("deleted") or post.get("hide") or post.get("closed"):
                    continue
                seen.add(post_id)
                candidates.append(post)
            if len(candidates) >= filters.limit * 2:
                break
            time.sleep(0.4)

        jobs: list[dict[str, Any]] = []
        for post in candidates:
            post_id = str(post.get("id"))
            title = clean_text(post.get("title") or "")
            summary = clean_text(post.get("summary") or "")
            tags, groups = self._tags(post)
            if not filters.match(f"{title} {summary} {' '.join(tags)}"):
                continue
            remote = self._is_remote(tags, groups, title, summary)
            city = self._city(groups) or ("远程" if remote else "")
            detail_text = self._detail_text(post_id) if len(jobs) < filters.limit else ""
            description = clean_text(detail_text or summary)
            # 地点判断用标题 + 结构化标签（招聘类型/工作方式），不看正文散文
            if not city_allowed(city, filters, title, " ".join(tags)):
                continue
            salary_text = self._salary(title) or ""
            user = post.get("user") or {}
            jobs.append(
                job_dict(
                    platform=self.name,
                    external_id=post_id,
                    title=title,
                    company="",  # 电鸭帖子多数是个人/小团队发帖，没有公司名，不编
                    url=POST_URL.format(post_id=post_id),
                    description=description,
                    salary_text=salary_text,
                    city=city,
                    tags=tags,
                    extra={
                        "hire_type": groups.get("hire_type", ""),
                        "job_type": groups.get("job_type", ""),
                        "field": groups.get("field", ""),
                        "city_tag": groups.get("city", ""),
                        "published_at": post.get("published_at"),
                        "comments_count": post.get("comments_count"),
                        "poster": (user.get("nickname") if isinstance(user, dict) else "") or "",
                        "summary": one_line(summary, 200),
                        "remote": remote,
                        "attribution": "来源 电鸭社区（https://eleduck.com）",
                    },
                )
            )
            if len(jobs) >= filters.limit:
                break
        return SourceResult(source=self.name, jobs=jobs, raw_count=raw_count,
                            note=f"分类「社区帖子招聘」，原始 {raw_count} 条")

    # ---------- 内部解析 ----------

    @staticmethod
    def _tags(post: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
        """返回（标签名列表，按标签组分好的字典）。"""
        names: list[str] = []
        groups: dict[str, str] = {}
        for tag in post.get("tags") or []:
            if not isinstance(tag, dict):
                continue
            name = clean_text(tag.get("name") or "")
            if not name:
                continue
            names.append(name)
            group = tag.get("tag_group") or {}
            code = (group.get("code") if isinstance(group, dict) else "") or ""
            if code and code not in groups:
                groups[code] = name
        return names, groups

    @staticmethod
    def _is_remote(tags: list[str], groups: dict[str, str], title: str,
                   summary: str) -> bool:
        if any(tag in REMOTE_TAGS for tag in tags):
            return True
        blob = f"{title} {summary}"
        return any(hint in blob for hint in ("远程", "兼职", "外包", "项目制",
                                             "线上", "remote", "Remote"))

    @staticmethod
    def _city(groups: dict[str, str]) -> str:
        city = groups.get("city", "")
        return "" if city in ("其他", "不限") else city

    @staticmethod
    def _salary(text: str) -> str:
        match = SALARY_RE.search(text or "")
        return match.group(0).strip() if match else ""

    @staticmethod
    def _detail_text(post_id: str) -> str:
        """取帖子正文。取不到就返回空串，让调用方退回摘要，不编内容。"""
        try:
            payload = http_json(DETAIL_API.format(post_id=post_id), retries=1)
        except SourceError:
            return ""
        node = payload.get("post") if isinstance(payload, dict) else None
        node = node if isinstance(node, dict) else (payload if isinstance(payload, dict) else {})
        return clean_text(node.get("raw_content") or node.get("content") or "")
