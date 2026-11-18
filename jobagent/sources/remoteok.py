"""RemoteOK 公开 JSON 接口。

官方在接口返回的第一条里写明使用条款：需要回链并注明来源，所以我们保留
homepage 与 attribution 字段，报告里也要标注来源。
"""

from __future__ import annotations

from typing import Any

from ..textutil import fix_mojibake, html_to_text, strip_source_trailer
from .base import Filters, Source, SourceResult, http_json, job_dict

API = "https://remoteok.com/api"


class RemoteOKSource(Source):
    name = "remoteok"
    label = "RemoteOK（远程岗位）"
    homepage = "https://remoteok.com"
    docs = "https://remoteok.com/api"
    notes = "公开 JSON；使用条款要求回链并注明来源；薪资为美元年薪"
    requires_login = False

    def fetch(self, filters: Filters) -> SourceResult:
        data = http_json(API)
        if not isinstance(data, list):
            return SourceResult(source=self.name, note="接口结构变了：顶层不是列表")
        items = [item for item in data if isinstance(item, dict) and item.get("id")]
        jobs: list[dict[str, Any]] = []
        for item in items:
            title = item.get("position") or ""
            tags = [str(t) for t in (item.get("tags") or [])]
            blob = f"{title} {' '.join(tags)} {item.get('company') or ''}"
            if not filters.match(blob):
                continue
            title = fix_mojibake(title)
            salary_min = item.get("salary_min") or 0
            salary_max = item.get("salary_max") or 0
            salary_text = ""
            if salary_max:
                salary_text = f"年 ${salary_min:,}–{salary_max:,}（RemoteOK）"
            location = item.get("location") or "远程"
            jobs.append(
                job_dict(
                    platform=self.name,
                    external_id=str(item.get("id")),
                    title=title,
                    company=fix_mojibake(item.get("company") or ""),
                    url=item.get("url") or item.get("apply_url") or "",
                    description=fix_mojibake(
                        strip_source_trailer(html_to_text(item.get("description")))
                    ),
                    salary_text=salary_text,
                    city=fix_mojibake(location),
                    tags=tags,
                    extra={
                        "date": item.get("date"),
                        "epoch": item.get("epoch"),
                        "salary_annual_usd": [salary_min or None, salary_max or None],
                        "attribution": "来源 RemoteOK（https://remoteok.com）",
                    },
                )
            )
            if len(jobs) >= filters.limit:
                break
        return SourceResult(source=self.name, jobs=jobs, raw_count=len(items),
                            note="RemoteOK 公开接口，需注明来源")
