"""Himalayas 公开接口（全球远程岗位）。

字段里有 locationRestrictions 和 timezoneRestrictions，这两项对判断
"这个远程岗位收不收中国时区的人"很关键，所以原样存进 extra，不臆测。
"""

from __future__ import annotations

from typing import Any

from ..textutil import clean_text, html_to_text
from .base import Filters, Source, SourceResult, http_json, job_dict

API = "https://himalayas.app/jobs/api"


class HimalayasSource(Source):
    name = "himalayas"
    label = "Himalayas（全球远程）"
    homepage = "https://himalayas.app"
    docs = API
    notes = ("公开 JSON；返回远程岗位；每条都带地区/时区限制，"
             "能不能投要看限制，报告里原样展示")
    city_scoped = False

    def fetch(self, filters: Filters) -> SourceResult:
        limit = max(10, min(50, filters.limit))
        data = http_json(API, params={"limit": limit})
        rows = (data or {}).get("jobs") or []
        jobs: list[dict[str, Any]] = []
        for item in rows:
            title = clean_text(item.get("title") or "")
            categories = [str(x) for x in (item.get("categories") or [])]
            blob = f"{title} {' '.join(categories)} {item.get('companyName') or ''}"
            if not filters.match(blob):
                continue
            restrictions = [str(x) for x in (item.get("locationRestrictions") or [])]
            low, high = item.get("minSalary"), item.get("maxSalary")
            currency = item.get("currency") or ""
            period = item.get("salaryPeriod") or ""
            salary_text = ""
            if low or high:
                salary_text = f"{low or '?'}–{high or '?'} {currency}/{period}".strip()
            city = "远程（" + "、".join(restrictions) + "）" if restrictions else "远程"
            jobs.append(
                job_dict(
                    platform=self.name,
                    external_id=str(item.get("guid") or item.get("applicationLink") or title),
                    title=title,
                    company=clean_text(item.get("companyName") or ""),
                    url=item.get("applicationLink") or "",
                    description=html_to_text(item.get("description") or item.get("excerpt")),
                    salary_text=salary_text,
                    city=city,
                    tags=[tag for tag in (item.get("employmentType"), *categories) if tag],
                    extra={
                        "location_restrictions": restrictions,
                        "timezone_restrictions": item.get("timezoneRestrictions"),
                        "seniority": item.get("seniority"),
                        "pub_date": item.get("pubDate"),
                        "attribution": "来源 Himalayas（https://himalayas.app）",
                    },
                )
            )
            if len(jobs) >= filters.limit:
                break
        return SourceResult(source=self.name, jobs=jobs, raw_count=len(rows),
                            note=f"公开接口 {len(rows)} 条远程岗位，带地区限制")
