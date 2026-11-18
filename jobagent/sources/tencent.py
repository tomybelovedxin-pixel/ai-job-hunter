"""腾讯招聘（官方公开接口）。

接口就是 careers.tencent.com 官网自己的取数接口，无需登录、无验证码。
字段来自实测响应：RecruitPostName / LocationName / Responsibility / PostURL 等。
薪水官网不公开，所以这里不填，不猜。
"""

from __future__ import annotations

from typing import Any

from ..textutil import clean_text
from .base import Filters, Source, SourceError, SourceResult, http_json, job_dict

QUERY_API = "https://careers.tencent.com/tencentcareer/api/post/Query"
DETAIL_API = "https://careers.tencent.com/tencentcareer/api/post/ByPostId"


class TencentSource(Source):
    name = "tencent"
    label = "腾讯招聘"
    homepage = "https://careers.tencent.com"
    docs = "https://careers.tencent.com/tencentcareer/api/post/Query"
    notes = "官网公开接口，无需登录；薪资不公开；城市按官网城市名过滤"
    city_scoped = True

    def fetch(self, filters: Filters) -> SourceResult:
        keyword = filters.keywords[0] if filters.keywords else ""
        page_size = max(10, min(100, filters.limit))
        jobs: list[dict[str, Any]] = []
        raw_count = 0
        for page in range(1, max(1, filters.pages) + 1):
            data = http_json(
                QUERY_API,
                params={
                    "timestamp": "1",
                    "countryId": "",
                    "cityId": "",
                    "bgIds": "",
                    "productId": "",
                    "categoryId": "",
                    "parentCategoryId": "",
                    "attrId": "",
                    "keyword": keyword,
                    "pageIndex": page,
                    "pageSize": page_size,
                    "language": "zh-cn",
                    "area": "cn",
                },
            )
            posts = ((data or {}).get("Data") or {}).get("Posts") or []
            if not posts:
                break
            raw_count += len(posts)
            for post in posts:
                title = post.get("RecruitPostName") or ""
                location = post.get("LocationName") or ""
                responsibility = clean_text(post.get("Responsibility") or "")
                if not filters.match(f"{title} {responsibility}"):
                    continue
                if filters.cities and location:
                    wants_remote = any("远程" in c for c in filters.cities)
                    hit_city = any(c and "远程" not in c and c in location
                                   for c in filters.cities)
                    if not hit_city:
                        # 只有岗位本身写着远程，才允许放过城市限制
                        is_remote = "远程" in location or "remote" in location.lower()
                        if not (wants_remote and is_remote):
                            continue
                jobs.append(
                    job_dict(
                        platform=self.name,
                        external_id=str(post.get("PostId") or post.get("RecruitPostId") or ""),
                        title=title,
                        company="腾讯",
                        url=post.get("PostURL") or "",
                        description=responsibility,
                        city=location,
                        experience=post.get("RequireWorkYearsName") or "",
                        education="",
                        tags=[tag for tag in (post.get("BGName"), post.get("CategoryName"),
                                              post.get("ProductName")) if tag],
                        extra={
                            "post_id": post.get("PostId"),
                            "bg": post.get("BGName"),
                            "updated": post.get("LastUpdateTime"),
                            "is_valid": post.get("IsValid"),
                        },
                    )
                )
                if len(jobs) >= filters.limit:
                    break
            if len(jobs) >= filters.limit:
                break
        return SourceResult(source=self.name, jobs=jobs, raw_count=raw_count,
                            note=f"关键词「{keyword}」，翻 {filters.pages} 页")

    def fetch_detail(self, post_id: str) -> dict[str, str]:
        """官网详情接口：补任职要求。"""
        data = http_json(DETAIL_API, params={"postId": post_id, "language": "zh-cn"})
        payload = (data or {}).get("Data") or {}
        if not payload:
            raise SourceError(f"详情接口没有返回 Data（postId={post_id}）")
        return {
            "responsibility": clean_text(payload.get("Responsibility") or ""),
            "requirement": clean_text(payload.get("Requirement") or ""),
            "title": payload.get("RecruitPostName") or "",
        }
