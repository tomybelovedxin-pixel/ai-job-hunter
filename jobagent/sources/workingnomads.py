"""Working Nomads 公开接口（全球远程岗位）。

接口是站点前端自己用的那个，一次返回全量在招远程岗位。
岗位以欧美公司为主，很多只在特定地区招人，所以地点一律写清楚，
不把它当成"一定能投"的岗位。
"""

from __future__ import annotations

from typing import Any

from ..textutil import clean_text, html_to_text
from .base import Filters, Source, SourceResult, http_json, job_dict

API = "https://www.workingnomads.com/api/exposed_jobs/"


class WorkingNomadsSource(Source):
    name = "workingnomads"
    label = "Working Nomads（全球远程）"
    homepage = "https://www.workingnomads.com"
    docs = API
    notes = "公开 JSON；一次返回全量远程岗位；以欧美远程岗位为主，多数限地区；薪资不公开"
    city_scoped = False

    def fetch(self, filters: Filters) -> SourceResult:
        data = http_json(API)
        if not isinstance(data, list):
            return SourceResult(source=self.name, note="接口结构变了：顶层不是列表")
        jobs: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            title = clean_text(item.get("title") or "")
            raw_tags = item.get("tags") or []
            if isinstance(raw_tags, str):  # 这个接口的 tags 是逗号分隔的字符串
                raw_tags = [part.strip() for part in raw_tags.split(",") if part.strip()]
            tags = [str(t) for t in raw_tags]
            category = clean_text(item.get("category_name") or "")
            location = clean_text(item.get("location") or "")
            blob = f"{title} {category} {' '.join(tags)} {item.get('company_name') or ''}"
            if not filters.match(blob):
                continue
            jobs.append(
                job_dict(
                    platform=self.name,
                    external_id=item.get("url") or title,
                    title=title,
                    company=clean_text(item.get("company_name") or ""),
                    url=item.get("url") or "",
                    description=html_to_text(item.get("description") or ""),
                    city=f"远程（{location}）" if location else "远程",
                    tags=[tag for tag in (category, *tags) if tag],
                    extra={
                        "pub_date": item.get("pub_date"),
                        "attribution": "来源 Working Nomads（https://www.workingnomads.com）",
                    },
                )
            )
            if len(jobs) >= filters.limit:
                break
        return SourceResult(source=self.name, jobs=jobs, raw_count=len(data),
                            note=f"公开接口 {len(data)} 条远程岗位")
