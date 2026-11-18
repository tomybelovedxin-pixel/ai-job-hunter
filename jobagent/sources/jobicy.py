"""Jobicy 公开接口（远程岗位聚合）。"""

from __future__ import annotations

from typing import Any

from ..textutil import html_to_text
from .base import Filters, Source, SourceResult, http_json, job_dict

API = "https://jobicy.com/api/v2/remote-jobs"


class JobicySource(Source):
    name = "jobicy"
    label = "Jobicy（远程岗位）"
    homepage = "https://jobicy.com"
    docs = "https://jobi.cy/apidocs"
    notes = "公开接口，要求注明来源；一次最多取 50 条"

    def fetch(self, filters: Filters) -> SourceResult:
        count = max(10, min(50, filters.limit))
        data = http_json(API, params={"count": count})
        items = (data or {}).get("jobs") or []
        jobs: list[dict[str, Any]] = []
        for item in items:
            title = item.get("jobTitle") or ""
            industry = item.get("jobIndustry") or []
            if isinstance(industry, str):
                industry = [industry]
            job_type = item.get("jobType") or []
            if isinstance(job_type, str):
                job_type = [job_type]
            blob = f"{title} {' '.join(map(str, industry))} {item.get('companyName') or ''}"
            if not filters.match(blob):
                continue
            salary_text = ""
            low, high = item.get("annualSalaryMin"), item.get("annualSalaryMax")
            currency = item.get("salaryCurrency") or "USD"
            if low or high:
                salary_text = f"年 {low or '?'}–{high or '?'} {currency}（Jobicy）"
            jobs.append(
                job_dict(
                    platform=self.name,
                    external_id=str(item.get("id")),
                    title=title,
                    company=item.get("companyName") or "",
                    url=item.get("url") or "",
                    description=html_to_text(item.get("jobDescription") or item.get("jobExcerpt")),
                    salary_text=salary_text,
                    city=item.get("jobGeo") or "远程",
                    tags=[str(x) for x in list(industry) + list(job_type)],
                    extra={
                        "level": item.get("jobLevel"),
                        "pubDate": item.get("pubDate"),
                        "attribution": "来源 Jobicy（https://jobicy.com）",
                    },
                )
            )
            if len(jobs) >= filters.limit:
                break
        return SourceResult(source=self.name, jobs=jobs, raw_count=len(items),
                            note="Jobicy 公开接口，需注明来源")
