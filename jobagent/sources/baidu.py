"""百度招聘（官网公开接口）。

接口是 talent.baidu.com 官网自己用的那个，只收表单提交，并且校验 Referer，
所以请求头按实测值写死在这里。字段来自实测响应：
name / workPlace / workContent / serviceCondition / education / workYears。
只公开社招（SOCIAL），薪资官网不公开，这里不填。
"""

from __future__ import annotations

from typing import Any

from ..textutil import clean_text
from .base import (Filters, Source, SourceResult, city_allowed, http_post_json,
                   job_dict)

API = "https://talent.baidu.com/httservice/getPostListNew"
DETAIL_URL = "https://talent.baidu.com/jobs/social-detail?postId={post_id}&recruitType=SOCIAL"

HEADERS = {
    "Referer": "https://talent.baidu.com/jobs/social-list",
    "Origin": "https://talent.baidu.com",
    "X-Requested-With": "XMLHttpRequest",
}


class BaiduSource(Source):
    name = "baidu"
    label = "百度招聘"
    homepage = "https://talent.baidu.com/jobs/social-list"
    docs = API
    notes = ("官网公开接口（表单提交，校验 Referer）；无需登录；只有社招；"
             "薪资不公开；外地坐班岗位按地点策略丢弃")
    city_scoped = False

    def fetch(self, filters: Filters) -> SourceResult:
        keywords = [k for k in (filters.keywords or []) if k][:4]
        jobs: list[dict[str, Any]] = []
        seen: set[str] = set()
        raw_count = 0
        errors: list[str] = []

        for keyword in keywords or [""]:
            for page in range(1, max(1, filters.pages) + 1):
                try:
                    payload = http_post_json(
                        API,
                        form={
                            "recruitType": "SOCIAL",
                            "pageSize": max(10, min(30, filters.limit)),
                            "curPage": page,
                            "keyWord": keyword,
                        },
                        extra_headers=HEADERS,
                    )
                except Exception as exc:
                    errors.append(f"关键词「{keyword}」第 {page} 页：{exc}")
                    break
                data = (payload or {}).get("data") or {}
                rows = data.get("list") or []
                if not rows:
                    break
                raw_count += len(rows)
                for row in rows:
                    post_id = str(row.get("postId") or "")
                    if not post_id or post_id in seen:
                        continue
                    title = clean_text(row.get("name") or "")
                    city = clean_text(row.get("workPlace") or "")
                    requirement = clean_text(row.get("serviceCondition") or "")
                    content = clean_text(row.get("workContent") or "")
                    if not title:
                        continue
                    blob = f"{title} {content} {requirement}"
                    if not filters.match(blob):
                        continue
                    # 地点判断只用标题和岗位标签：JD 正文里的"线上"是业务用语，
                    # 不能当作"这个岗位能远程"的证据
                    if not city_allowed(city, filters, title,
                                        clean_text(row.get("postType") or "")):
                        continue
                    seen.add(post_id)
                    jobs.append(
                        job_dict(
                            platform=self.name,
                            external_id=post_id,
                            title=title,
                            company="百度",
                            url=DETAIL_URL.format(post_id=post_id),
                            description=clean_text(
                                f"工作内容：\n{content}\n\n任职要求：\n{requirement}"
                            ),
                            city=city,
                            experience=clean_text(row.get("workYears") or ""),
                            education=clean_text(row.get("education") or ""),
                            tags=[tag for tag in
                                  (clean_text(row.get("bgShortName") or ""),
                                   clean_text(row.get("postType") or ""),
                                   clean_text(row.get("orgName") or "")) if tag],
                            extra={
                                "job_id": row.get("jobId"),
                                "publish_date": row.get("publishDate"),
                                "update_date": row.get("updateDate"),
                                "recruit_num": row.get("recruitNum"),
                                "attribution": "来源 百度招聘（https://talent.baidu.com）",
                            },
                        )
                    )
                    if len(jobs) >= filters.limit:
                        break
                if len(jobs) >= filters.limit:
                    break
            if len(jobs) >= filters.limit:
                break

        note = f"关键词 {keywords or ['(空)']}，原始 {raw_count} 条"
        if errors:
            note += "；部分请求失败：" + "；".join(errors[:3])
        return SourceResult(source=self.name, jobs=jobs, raw_count=raw_count, note=note)
