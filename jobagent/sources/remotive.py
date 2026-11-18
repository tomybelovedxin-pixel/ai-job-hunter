"""Remotive 公开接口（远程岗位聚合）。"""

from __future__ import annotations

from typing import Any

from ..textutil import html_to_text
from .base import Filters, Source, SourceResult, http_json, job_dict

API = "https://remotive.com/api/remote-jobs"


class RemotiveSource(Source):
    name = "remotive"
    label = "Remotive（远程岗位）"
    homepage = "https://remotive.com"
    docs = "https://remotive.com/api/remote-jobs"
    notes = "公开接口；不返回结构化薪资，薪资只出现在正文里"

    def fetch(self, filters: Filters) -> SourceResult:
        limit = max(5, min(50, filters.limit))
        query = filters.keywords[0] if filters.keywords else ""
        data = http_json(API, params={"limit": limit, "search": query} if query
                         else {"limit": limit})
        items = (data or {}).get("jobs") or []
        jobs: list[dict[str, Any]] = []
        for item in items:
            title = item.get("title") or ""
            category = item.get("category") or ""
            blob = f"{title} {category} {item.get('company_name') or ''}"
            if not filters.match(blob):
                continue
            jobs.append(
                job_dict(
                    platform=self.name,
                    external_id=str(item.get("id")),
                    title=title,
                    company=item.get("company_name") or "",
                    url=item.get("url") or "",
                    description=html_to_text(item.get("description")),
                    salary_text=(item.get("salary") or "").strip(),
                    city=item.get("candidate_required_location") or "远程",
                    tags=[str(x) for x in [category, item.get("job_type")] if x],
                    extra={
                        "publication_date": item.get("publication_date"),
                        "attribution": "来源 Remotive（https://remotive.com）",
                    },
                )
            )
            if len(jobs) >= filters.limit:
                break
        return SourceResult(source=self.name, jobs=jobs, raw_count=len(items),
                            note=f"Remotive 公开接口，检索词「{query}」")
