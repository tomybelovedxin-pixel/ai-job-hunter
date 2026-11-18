"""打分排序：把 JD 拆成能力要求，逐项对照候选人档案，给出可解释的分数。

规则是确定性的、能复盘：命中什么、缺什么、为什么降分，全部写进 reasons / gaps。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .profile import Profile

SALARY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[-~至]\s*(\d+(?:\.\d+)?)\s*[kK]")
SINGLE_SALARY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[kK]")
YEARS_RE = re.compile(r"(\d+)\s*[-~]?\s*(\d+)?\s*年")


@dataclass
class Score:
    total: float
    reasons: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    blocked: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.total, 1),
            "reasons": self.reasons,
            "gaps": self.gaps,
            "matched": self.matched,
            "blocked": self.blocked,
        }


def parse_salary(text: str | None) -> tuple[float | None, float | None]:
    """从 '8-13K·13薪' / '15K' 这类文本里取月薪区间（单位 K）。"""
    if not text:
        return None, None
    normalized = text.replace("·", " ").replace("薪", "")
    match = SALARY_RE.search(normalized)
    if match:
        return float(match.group(1)), float(match.group(2))
    single = SINGLE_SALARY_RE.search(normalized)
    if single:
        value = float(single.group(1))
        return value, value
    return None, None


def parse_required_years(*texts: str | None) -> int | None:
    """从经验要求里估一个最低年限；'经验不限' 之类返回 None。"""
    for text in texts:
        if not text:
            continue
        if any(word in text for word in ("不限", "应届", "无经验", "在校")):
            return None
        for match in YEARS_RE.finditer(text):
            years = int(match.group(1))
            if 0 < years <= 20:
                return years
    return None


def score_job(job: Any, profile: Profile) -> Score:
    title = (job["title"] if not isinstance(job, dict) else job.get("title")) or ""
    text = " ".join(
        str(x or "")
        for x in (
            title,
            job["description"] if not isinstance(job, dict) else job.get("description"),
            job["tags"] if not isinstance(job, dict) else job.get("tags"),
            job["company"] if not isinstance(job, dict) else job.get("company"),
        )
    )
    haystack = text.lower()
    title_lower = title.lower()

    score = Score(total=0.0)

    # 1. 硬红线：命中直接判不建议，不浪费时间
    for block in profile.hard_blocks:
        if block and block in haystack:
            score.total = 0.0
            score.blocked = True
            score.reasons.append(f"命中红线「{block}」")
            return score

    # 2. 能力命中：按证据等级加权，等级越高越值钱
    capability_points = 0.0
    max_points = sum(cap.level for cap in profile.capabilities) or 1
    for cap in profile.capabilities:
        hits = [kw for kw in cap.keywords if kw and kw in haystack]
        if not hits:
            continue
        unique_hits = list(dict.fromkeys(hits))[:4]
        weight = cap.level * (1 + 0.2 * (len(unique_hits) - 1))
        capability_points += weight
        score.matched.append(cap.name)
        score.reasons.append(f"{cap.name}（{cap.level} 级）命中：{'、'.join(unique_hits)}")

    # 3. 方向匹配：标题里直接对上，权重最高
    direction_hit = None
    for direction in profile.directions:
        if any(kw in title_lower for kw in direction["keywords"]):
            direction_hit = direction["name"]
            break
    direction_points = 28.0 if direction_hit else 0.0
    if direction_hit:
        score.reasons.append(f"岗位方向对上：{direction_hit}")

    # 4. 地点
    city_text = str((job["city"] if not isinstance(job, dict) else job.get("city")) or "")
    location_points = 0.0
    if any(c in text for c in profile.city_preferred) or "远程" in haystack:
        location_points = 12.0
        score.reasons.append("地点可接受（本地或远程）")
    elif city_text and city_text not in profile.city:
        location_points = -4.0
        score.gaps.append(f"工作地点在 {city_text}，需要确认能否远程")

    # 5. 年限：够不上的老实记成缺口
    years = parse_required_years(
        job["experience"] if not isinstance(job, dict) else job.get("experience"),
        job["description"] if not isinstance(job, dict) else job.get("description"),
    )
    years_penalty = 0.0
    if years is not None and years >= 3:
        years_penalty = min(18.0, 6.0 * years)
        score.gaps.append(f"岗位写的是 {years} 年以上经验，我按实情说明没有")

    # 6. 薪资下限
    salary_penalty = 0.0
    salary_min, salary_max = parse_salary(
        job["salary_text"] if not isinstance(job, dict) else job.get("salary_text")
    )
    if salary_max is not None and profile.salary_floor and salary_max < profile.salary_floor:
        salary_penalty = 20.0
        score.gaps.append(f"薪资上限 {salary_max:g}K 低于我的底线 {profile.salary_floor:g}K")

    # 7. 需要先确认的点（外包/驻场/年限等）
    for item in profile.soft_blocks:
        if item["pattern"] and item["pattern"] in haystack:
            if item["note"] not in score.gaps:
                score.gaps.append(item["note"])

    raw = (
        (capability_points / max_points) * 60.0
        + direction_points
        + location_points
        - years_penalty
        - salary_penalty
    )
    score.total = max(0.0, min(100.0, raw))
    if not score.matched:
        score.gaps.append("JD 里没抓到已建档的能力关键词，需要人工看一眼")
    return score
