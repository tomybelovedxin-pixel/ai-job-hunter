"""岗位去重与有效性判断。

三件事：
1. 同平台同 ID 的重复由数据库唯一键挡住（更新而不是新增）。
2. 跨平台重复：标题归一化 + 公司名相似度，超过阈值标记 dup_of。
3. 有效性/可疑度：明显过期、信息不足、疑似中介或骗局的记标记，不直接删。

相似度优先用 rapidfuzz，没装就退回 difflib，结果口径一致。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

TITLE_NOISE = re.compile(
    r"(急招|急聘|诚聘|招聘|高薪|双休|五险一金|应届|实习|全职|兼职|远程|remote|"
    r"\(.*?\)|（.*?）|【.*?】|\[.*?\])",
    re.I,
)
SPACES = re.compile(r"[\s\-_/,，、·]+")

SCAM_MARKERS = (
    "刷单", "押金", "保证金", "无需经验日结", "日结高薪", "下载APP", "加QQ",
    "加微信详聊", "包吃住月入", "打字员", "游戏代练",
)
VAGUE_MARKERS = ("待遇面议", "薪资面议", "详情面谈")
AGENCY_MARKERS = ("外包", "人力", "派遣", "中介", "猎头", "劳务")


def similarity(left: str, right: str) -> float:
    """0–100 的相似度。装了就用手写模糊库，没装用标准库。"""
    if not left or not right:
        return 0.0
    try:
        from rapidfuzz import fuzz

        return float(fuzz.token_set_ratio(left, right))
    except Exception:
        from difflib import SequenceMatcher

        return SequenceMatcher(None, left, right).ratio() * 100


def normalize_title(title: str) -> str:
    text = TITLE_NOISE.sub(" ", title or "")
    text = SPACES.sub("", text)
    return text.lower()


def normalize_company(company: str) -> str:
    text = (company or "").lower()
    for suffix in ("有限公司", "股份有限公司", "有限责任公司", "科技", "集团", "co.,ltd", "inc", "ltd"):
        text = text.replace(suffix, "")
    return SPACES.sub("", text)


def block_key(title: str, size: int = 6) -> str:
    """分块键：只用标题前几个字做候选分组，避免全表两两比较。"""
    return normalize_title(title)[:size]


def bucket_keys(title: str) -> list[str]:
    """一个标题进多个桶：前缀、后缀、字符集合指纹。命中任一桶即为候选对。"""
    norm = normalize_title(title)
    if not norm:
        return []
    keys = {norm[:5], norm[-5:], "".join(sorted(set(norm)))[:8]}
    return [key for key in keys if key]


def find_duplicates(rows: Iterable[Any], *, threshold: float = 92.0,
                    limit: int = 500) -> list[tuple[int, int, float]]:
    """全库找跨平台重复，返回 [(重复岗位 id, 保留岗位 id, 相似度)]。"""
    items = list(rows)
    buckets: dict[str, list[Any]] = {}
    for row in items:
        for key in bucket_keys(str(row["title"] or "")):
            buckets.setdefault(key, []).append(row)

    found: list[tuple[int, int, float]] = []
    seen: set[int] = set()
    for row in items:
        self_id = int(row["id"])
        if self_id in seen:
            continue
        candidates = {int(r["id"]): r for key in bucket_keys(str(row["title"] or ""))
                      for r in buckets.get(key, [])}
        candidates.pop(self_id, None)
        title = normalize_title(str(row["title"] or ""))
        company = normalize_company(str(row["company"] or ""))
        for other_id, other in candidates.items():
            if other_id in seen:
                continue
            score = similarity(title, normalize_title(str(other["title"] or "")))
            if score < threshold:
                continue
            other_company = normalize_company(str(other["company"] or ""))
            if company and other_company and similarity(company, other_company) < 85:
                continue
            keep, drop = sorted((self_id, other_id))
            found.append((drop, keep, score))
            seen.add(drop)
            if len(found) >= limit:
                return found
    return found


@dataclass
class DupCheck:
    dup_of: int | None = None
    reason: str = ""
    flags: list[str] = field(default_factory=list)
    valid: bool | None = None
    note: str = ""


def check_job(job: Any, *, existing: Iterable[Any] = (), self_id: int | None = None,
              now: float | None = None, stale_days: int = 45) -> DupCheck:
    """对一个岗位做重复与有效性判断。existing 是候选岗位行（已分块）。"""
    get = (lambda key, default=None: job[key] if not isinstance(job, dict) else job.get(key, default))
    title = str(get("title") or "")
    company = str(get("company") or "")
    description = str(get("description") or "")
    norm_title = normalize_title(title)
    norm_company = normalize_company(company)

    result = DupCheck()

    for row in existing:
        if self_id is not None and int(row["id"]) == int(self_id):
            continue
        other_title = normalize_title(str(row["title"] or ""))
        other_company = normalize_company(str(row["company"] or ""))
        if not norm_title or not other_title:
            continue
        title_score = similarity(norm_title, other_title)
        company_score = similarity(norm_company, other_company) if norm_company and other_company else 0
        if title_score >= 92 and company_score >= 85:
            result.dup_of = int(row["id"])
            result.reason = f"与 #{row['id']}「{row['title']}」高度重复（标题 {title_score:.0f}）"
            result.flags.append("跨平台重复")
            break

    blob = f"{title} {company} {description}"
    if any(marker in blob for marker in SCAM_MARKERS):
        result.valid = False
        result.flags.append("疑似虚假招聘")
        result.note = "出现刷单/押金/私加联系方式等风险词，建议人工确认后再考虑"
    elif not title or (not company and len(description) < 80):
        result.valid = False
        result.flags.append("信息不足")
        result.note = "缺少公司或职位描述过短，无法判断"
    elif any(marker in blob for marker in VAGUE_MARKERS) and len(description) < 200:
        result.valid = None
        result.flags.append("信息模糊")
        result.note = "薪资与职责都写得含糊，建议先问清楚"
    if any(marker in blob for marker in AGENCY_MARKERS):
        result.flags.append("疑似外包/派遣")
    if len(description) < 120:
        result.flags.append("JD 偏短")

    last_seen = str(get("last_seen") or "") or (time.strftime("%Y-%m-%d %H:%M:%S"))
    try:
        seen = time.mktime(time.strptime(last_seen[:19], "%Y-%m-%d %H:%M:%S"))
        age_days = ((now or time.time()) - seen) / 86400
        if age_days > stale_days:
            result.flags.append("较久未见更新")
            if result.valid is None:
                result.valid = None
                result.note = result.note or f"最近一次抓到是 {age_days:.0f} 天前，建议确认是否还在招"
    except Exception:
        pass
    return result
