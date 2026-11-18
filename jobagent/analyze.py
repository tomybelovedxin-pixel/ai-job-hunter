"""岗位分析引擎：把一份 JD 拆成要求项，逐条对照能力档案，给出可解释的结论。

评分口径写在 SCORE_RULES 里，命中和缺口都会输出，不接受"因为标题带 AI 所以合适"
这种判断。数据来源只有两处：岗位文本 + 能力等级表。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ROOT
from .profile import Profile

GRADES_PATH = ROOT / "capability_grades.json"
PROFILE_PATH = ROOT / "profile.json"

SCORE_RULES = {
    "base": 40,
    "requirement_weight": 45,   # 要求覆盖率占分
    "direction": 10,            # 方向对不对
    "location": 8,              # 地点可接受
    "location_penalty": -6,     # 地点明显不合适
    "salary_penalty": -12,      # 薪资低于底线
    "years_step": -6,           # 经验年限每超一年
    "years_cap": -18,
    "education_penalty": -10,   # 学历硬门槛
    # JD 信息量不足时（社区帖、只有一句话的岗位），不给高置信度
    "low_info_max_score": 70,
    "low_info_min_requirements": 3,   # 至少拆出 3 类不同能力，否则算信息量不足
    "low_info_min_chars": 500,
    "verdicts": [
        (70, "大概率胜任"),
        (55, "需先验证"),
        (0, "暂不胜任"),
    ],
}

CORE_MARKERS = ("必须", "精通", "熟练掌握", "独立负责", "独立完成", "深入理解", "至少",
                "要求", "负责搭建", "需要有")
# 出现这些词，说明岗位可以异地做（兼职、远程、项目制、线上）
FLEXIBLE_MARKERS = ("兼职", "远程", "线上", "项目制", "按单", "按项目", "外包", "在家",
                    "part-time", "part time", "freelance", "contract", "remote")
REQUIREMENT_HEADINGS = ("任职要求", "岗位要求", "职位要求", "任职资格", "要求", "资格条件",
                        "requirements", "qualifications", "我们希望")
DUTY_HEADINGS = ("岗位职责", "工作内容", "职位描述", "职责描述", "工作职责", "主要职责",
                 "responsibilities", "what you")
EDUCATION_HARD = ("硕士", "研究生", "博士", "985", "211", "双一流")
SPLIT = re.compile(r"[\n；;•·]|(?:^|\s)\d+[\.、)]\s*")

# 明显不是能力要求的句子：公司介绍、福利、号召投递
JUNK = re.compile(
    r"^(about us|about the|we are|we're|join us|apply|apply now|tell us|who should|"
    r"what we|why |benefits|perks|compensation|equal opportunity|our |at [A-Z]|"
    r"about [A-Z]|in \d{4}|remote\b|location\b|"
    r"(key|main|core)?\s*(responsibilit|requirement|qualification|duties)|"
    r"https?://|www\.|read more|click |follow )",
    re.I,
)
# 中文里同样不是能力要求的句子：薪酬福利、公司介绍、投递流程、到岗时间
JUNK_ZH = re.compile(
    r"^(薪酬|薪资|薪水|待遇|福利|月薪|年薪|日报酬|报酬|劳务费|底薪|提成|补贴|奖金|"
    r"五险|社保|公积金|餐补|包吃|包住|上班时间|工作时间|休息|双休|单休|"
    r"地点|地址|坐标|联系人|简历|投递|应聘|面试|入职|转正|试用|"
    r"关于我们|公司简介|公司介绍|我们是一家|我们是|团队介绍|项目背景|公司背景|"
    r"备注|说明|加分项说明)"
    r"|(元\s*/\s*(月|天|时|日|次))|(税前|税后)|(五险一金)|(K\s*/\s*月)|"
    r"(岗位描述来源于)|(信息来源)"
)
# 像能力要求的句子：明确在讲技能门槛，而不是公司介绍
SKILL_HINT = re.compile(
    r"(experience|proficien|familiar with|knowledge of|ability to|skilled|expertise|years of|"
    r"strong (in|with)|comfortable with|hands-on|"
    r"精通|熟悉|熟练|了解|掌握|具备|负责|参与|经验|能力|会|能用|独立|"
    r"python|sql|api|agent|llm|rag|prompt|docker|git|测试|接口|脚本|自动化|"
    r"至少|要求)",
    re.I,
)


@dataclass
class Requirement:
    text: str
    section: str = "要求"
    capability: str = ""
    grade: str = "D"
    basis: str = ""
    limit: str = ""
    verdict: str = "证据不足"
    weight: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text, "section": self.section, "capability": self.capability,
            "grade": self.grade, "basis": self.basis, "verdict": self.verdict,
            "limit": self.limit, "weight": round(self.weight, 2),
        }


def load_grades(path: str | Path = GRADES_PATH) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {"capabilities": {}}


def extract_requirements(text: str) -> list[Requirement]:
    """按行和分隔符切出要求项，并标出它来自职责段还是要求段。"""
    text = (text or "").replace("\r\n", "\n")
    section = "要求"
    items: list[Requirement] = []
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        head = line[:20]
        if any(word in head for word in REQUIREMENT_HEADINGS):
            section = "要求"
            line = re.sub("|".join(REQUIREMENT_HEADINGS) + r"[：: ]*", "", line).strip()
            if not line:
                continue
        elif any(word in head for word in DUTY_HEADINGS):
            section = "职责"
            line = re.sub("|".join(DUTY_HEADINGS) + r"[：: ]*", "", line).strip()
            if not line:
                continue
        for part in SPLIT.split(line):
            part = (part or "").strip(" ·-—\t")
            if len(part) < 6 or len(part) > 220:
                continue
            if re.fullmatch(r"[\d\W]+", part):
                continue
            if JUNK.match(part) or JUNK_ZH.search(part):
                continue
            items.append(Requirement(text=part, section=section))
    return items


def _is_requirement_like(item: Requirement) -> bool:
    """判断一条句子值不值得计入要求覆盖率：命中能力词或技能动词才算。"""
    return bool(item.capability) or bool(SKILL_HINT.search(item.text))


_ASCII_KEYWORD_CACHE: dict[str, re.Pattern[str]] = {}


def _keyword_hit(keyword: str, lowered: str) -> bool:
    """短英文关键词按词匹配，别让关键词 es 命中 Kubernetes 里的 es。"""
    if keyword.isascii() and keyword.replace("-", "").replace("/", "").isalnum():
        pattern = _ASCII_KEYWORD_CACHE.get(keyword)
        if pattern is None:
            pattern = re.compile(r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])")
            _ASCII_KEYWORD_CACHE[keyword] = pattern
        return bool(pattern.search(lowered))
    return keyword in lowered


def _match_capability(text: str, profile: Profile) -> tuple[str, int, str]:
    lowered = text.lower()
    best_name, best_hits, best_keywords = "", 0, []
    for cap in profile.capabilities:
        hits = [kw for kw in cap.keywords if kw and _keyword_hit(kw.lower(), lowered)]
        if len(hits) > best_hits:
            best_name, best_hits, best_keywords = cap.name, len(hits), hits
    if not best_name:
        return "", 0, []
    evidence = ""
    cap = profile.capability(best_name)
    if cap:
        evidence = cap.evidence
    return best_name, best_hits, best_keywords or [evidence]


def _detect_direction(text: str, profile: Profile) -> tuple[str, float]:
    lowered = text.lower()
    for direction in profile.directions:
        keywords = [str(k).lower() for k in direction.get("keywords", [])]
        if any(keyword and keyword in lowered for keyword in keywords):
            return direction.get("name", ""), SCORE_RULES["direction"]
    return "", 0.0


def analyze_text(
    text: str,
    *,
    title: str = "",
    company: str = "",
    city: str = "",
    salary_text: str = "",
    experience: str = "",
    education: str = "",
    profile: Profile | None = None,
    grades: dict[str, Any] | None = None,
    engine: str = "rule",
) -> dict[str, Any]:
    profile = profile or Profile.load(PROFILE_PATH)
    grades = grades or load_grades()
    grade_table = grades.get("capabilities", {})
    blob = f"{title}\n{text}"
    lowered = blob.lower()

    requirements = extract_requirements(text)
    requirement_items: list[Requirement] = []
    ignored: list[str] = []
    for item in requirements:
        name, hits, keywords = _match_capability(item.text, profile)
        entry = grade_table.get(name, {}) if name else {}
        grade = entry.get("grade", "D") if name else "D"
        item.capability = name
        item.grade = grade if name else "D"
        item.basis = entry.get("basis", "") if name else ""
        item.limit = entry.get("limit", "") if name else ""
        item.verdict = {
            "A": "已具备（有可复核证据）", "B": "部分具备", "C": "只了解，没有落地",
            "D": "没有证据或目前不具备",
        }.get(item.grade, "证据不足")
        item.weight = 2.0 if any(marker in item.text for marker in CORE_MARKERS) else 1.0
        if not _is_requirement_like(item):
            ignored.append(item.text)
            continue
        requirement_items.append(item)
    requirement_items = requirement_items[:14]

    # 硬红线
    blocked = [word for word in profile.hard_blocks if word and word in lowered]

    # 要求覆盖率：按"不同能力"算，同一个能力在多条要求里出现只算一次，
    # 否则一份 JD 反复提 Python 就能刷到满分。
    value = {"A": 1.0, "B": 0.6, "C": 0.2, "D": 0.0}
    covered: dict[str, str] = {}
    for item in requirement_items:
        key = item.capability or "未归类"
        current = covered.get(key)
        if current is None or value.get(item.grade, 0.0) > value.get(current, 0.0):
            covered[key] = item.grade
    coverage = sum(value.get(grade, 0.0) for grade in covered.values())
    coverage_ratio = coverage / max(1, len(covered))

    direction, direction_score = _detect_direction(blob, profile)

    city_text = f"{city} {lowered}"
    location_score = 0.0
    location_note = ""
    remote_hint = ("远程" in city_text) or ("remote" in city_text.lower()) or ("远程" in lowered)
    flexible_hit = next((word for word in FLEXIBLE_MARKERS if word in lowered), "")
    if (remote_hint or flexible_hit) and profile.remote_ok:
        location_score = SCORE_RULES["location"]
        location_note = ("岗位可远程或兼职，地点不受限" if flexible_hit
                         else "岗位可远程，符合你的接受范围")
    elif any(place in city_text for place in profile.city_preferred if place):
        location_score = SCORE_RULES["location"]
        location_note = "地点在你的可接受范围内"
    elif city and city not in profile.city_preferred:
        location_score = SCORE_RULES["location_penalty"]
        location_note = f"地点在 {city}，需要确认能否远程"

    years = _required_years(experience, text)
    years_score = 0.0
    years_note = ""
    if years and years >= 3:
        years_score = max(SCORE_RULES["years_cap"], SCORE_RULES["years_step"] * (years - 2))
        years_note = f"岗位要求 {years} 年以上经验，我没有企业任职年限，需要按实情沟通"

    salary_low, salary_high, currency = _parse_salary(salary_text)
    salary_score = 0.0
    salary_note = ""
    if salary_high is not None and currency == "CNY" and profile.salary_floor:
        if salary_high < profile.salary_floor:
            salary_score = SCORE_RULES["salary_penalty"]
            salary_note = f"薪资上限 {salary_high:g}K 低于你设定的底线 {profile.salary_floor:g}K"
    elif currency and currency != "CNY":
        salary_note = "薪资是外币或年薪口径，先按原值看，换算后再判断"

    education_hit = [word for word in EDUCATION_HARD if word in blob]
    education_score = SCORE_RULES["education_penalty"] if education_hit else 0.0

    raw_score = (SCORE_RULES["base"]
                 + coverage_ratio * SCORE_RULES["requirement_weight"]
                 + direction_score + location_score + years_score + salary_score
                 + education_score)
    score = 0.0 if blocked else max(0.0, min(100.0, raw_score))
    # 信息量不足：要求项太少或正文太短，压分并降级结论，避免"关键词多就高分"
    enough_info = (len(covered) >= SCORE_RULES["low_info_min_requirements"]
                   and len(text) >= SCORE_RULES["low_info_min_chars"])
    if not blocked and not enough_info:
        score = min(score, float(SCORE_RULES["low_info_max_score"]))

    if blocked:
        verdict = "暂不胜任"
        verdict_reason = "命中你设的红线：" + "、".join(blocked)
    else:
        verdict = next(name for threshold, name in SCORE_RULES["verdicts"] if score >= threshold)
        verdict_reason = f"要求覆盖率 {coverage_ratio:.0%}，综合分 {score:.0f}"
        if not enough_info:
            verdict = "需先验证" if verdict != "暂不胜任" else verdict
            verdict_reason += (f"；JD 信息量不足（只拆出 {len(covered)} 类能力要求、"
                               f"{len(text)} 字），先要到完整 JD 再判断")
        if education_hit:
            verdict_reason += f"；学历门槛（{'、'.join(education_hit[:2])}）够不上"
            if verdict == "大概率胜任":
                verdict = "需先验证"
        core_missing = [item for item in requirement_items
                        if item.grade == "D" and item.weight >= 2]
        if core_missing and verdict == "大概率胜任":
            verdict = "需先验证"
            verdict_reason += f"；有 {len(core_missing)} 条核心要求没有证据支撑"

    matched = [item.as_dict() for item in requirement_items if item.grade in ("A", "B")]
    partial = [item.as_dict() for item in requirement_items if item.grade == "B"]
    missing = [item.as_dict() for item in requirement_items if item.grade in ("C", "D")]

    gaps: list[str] = []
    if years_note:
        gaps.append(years_note)
    if education_score:
        gaps.append("学历/院校门槛：" + "、".join(education_hit[:3]))
    if salary_note:
        gaps.append(salary_note)
    if location_score < 0 and location_note:
        gaps.append(location_note)
    for item in missing[:6]:
        gaps.append(f"{item['text'][:60]}（{item['verdict']}）")

    strengths: list[str] = []
    limits: list[str] = []
    for item in requirement_items:
        if item.grade in ("A", "B") and item.capability:
            if item.basis:
                strengths.append(f"{item.capability}：{item.basis[:70]}")
            if item.limit:
                limits.append(f"{item.capability}的边界：{item.limit}")
    strengths = list(dict.fromkeys(strengths))[:6]
    limits = list(dict.fromkeys(limits))[:4]
    if direction:
        strengths.append(f"岗位方向与你设定的方向一致：{direction}")

    risks: list[str] = []
    risks.extend(limits)
    if not blocked and not enough_info:
        risks.append("JD 不完整（社区帖或简短描述），当前判断只是初筛，需要拿到完整 JD 再复核")
    if salary_score < 0:
        risks.append("薪资口径不达标，投入时间可能不划算")
    if years >= 3:
        risks.append("年限要求明确，简历需要通过项目证据补足说服力")
    if not requirement_items:
        risks.append("JD 正文太短，要求项没拆出来，判断依据不足")
    if missing and len(missing) >= len(requirement_items) * 0.5:
        risks.append("超过一半要求没有证据支撑，先补能力再投更合适")

    needed_skills = _needed_skills(lowered, profile)
    job_problem = [item.text for item in requirement_items if item.section == "职责"][:4] \
        or [line.strip() for line in text.split("\n") if line.strip()][:3]

    advice: list[str] = []
    if verdict in ("大概率胜任", "需先验证"):
        advice.append("值得投：先按这个方向定制简历，重点写与要求项对应的项目证据")
    else:
        advice.append("暂不优先投：先把缺口补上，或改投要求更贴近你证据的岗位")
    if gaps:
        advice.append("投递前先问清楚：" + "；".join(gaps[:2]))
    if partial:
        advice.append("有部分具备的项，面试时主动说明做到哪一步、哪里还没验证")

    return {
        "engine": engine,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "fit_score": round(score, 1),
        "coverage_ratio": round(coverage_ratio, 3),
        "job_problem": job_problem,
        "needed_skills": needed_skills,
        "requirements": [item.as_dict() for item in requirement_items],
        "matched": matched,
        "partial": partial,
        "missing": missing,
        "strengths": strengths,
        "risks": risks,
        "gaps": gaps,
        "questions": list(profile.questions)[:5],
        "checks": {
            "direction": {"value": direction, "score": direction_score},
            "location": {"note": location_note, "score": location_score, "city": city,
                         "flexible": flexible_hit},
            "years": {"value": years, "note": years_note, "score": years_score},
            "salary": {"low": salary_low, "high": salary_high, "currency": currency,
                       "note": salary_note, "score": salary_score},
            "education": {"hit": education_hit, "score": education_score},
            "blocked": blocked,
        },
        "advice": advice,
        "ignored_lines": ignored[:20],
        "rules": SCORE_RULES,
    }


def analyze_job(job: Any, *, profile: Profile | None = None,
                grades: dict[str, Any] | None = None) -> dict[str, Any]:
    get = (lambda key, default="": job[key] if not isinstance(job, dict) else job.get(key, default))
    return analyze_text(
        # 有中文译文就按译文分析，英文原文仍留在库里
        str(get("description_zh") or get("description") or ""),
        title=str(get("title") or ""),
        company=str(get("company") or ""),
        city=str(get("city") or ""),
        salary_text=str(get("salary_text") or ""),
        experience=str(get("experience") or ""),
        education=str(get("education") or ""),
        profile=profile,
        grades=grades,
    )


def _needed_skills(lowered: str, profile: Profile) -> list[str]:
    counts: dict[str, int] = {}
    for cap in profile.capabilities:
        for keyword in cap.keywords:
            keyword = (keyword or "").lower()
            if keyword and keyword in lowered:
                counts[keyword] = counts.get(keyword, 0) + 1
    return [word for word, _ in sorted(counts.items(), key=lambda item: -item[1])][:12]


def _required_years(*texts: str | None) -> int:
    for text in texts:
        if not text:
            continue
        if any(word in text for word in ("不限", "应届", "无经验", "在校")):
            return 0
        for match in re.finditer(r"(\d+)\s*[-~]?\s*(\d+)?\s*年", text):
            years = int(match.group(1))
            if 0 < years <= 20:
                return years
    return 0


def _parse_salary(text: str | None) -> tuple[float | None, float | None, str]:
    if not text:
        return None, None, ""
    normalized = text.replace("·", " ").replace("薪", " ")
    if "$" in text or "USD" in text.upper():
        numbers = [float(x.replace(",", "")) for x in re.findall(r"[\d,]{3,}", normalized)
                   if x.replace(",", "").isdigit()]
        numbers = [n for n in numbers if n > 0]
        if numbers:
            return min(numbers), max(numbers), "USD"
        return None, None, "USD"
    match = re.search(r"(\d+(?:\.\d+)?)\s*[-~至]\s*(\d+(?:\.\d+)?)\s*[kK]", normalized)
    if match:
        return float(match.group(1)), float(match.group(2)), "CNY"
    single = re.search(r"(\d+(?:\.\d+)?)\s*[kK]", normalized)
    if single:
        value = float(single.group(1))
        return value, value, "CNY"
    wan = re.search(r"(\d+(?:\.\d+)?)\s*[-~至]?\s*(\d+(?:\.\d+)?)?\s*万", normalized)
    if wan:
        low = float(wan.group(1)) * 10
        high = float(wan.group(2)) * 10 if wan.group(2) else low
        return low, high, "CNY"
    # "1000-1500元/月" 这种按元写的月薪也要认（2026-10-16：以前会当成没有薪资信息，
    # 低于底线的岗位因此漏进队列）；"元/天""元/时"是日薪/时薪，不是月薪，不能按月薪比，
    # 否则 "750-800元/天" 会被读成 0.8K 月薪然后被底线误杀（2026-10-18 实战修正）。
    yuan = re.search(r"(\d{3,6})\s*[-~至]\s*(\d{3,6})\s*元\s*/?\s*(月|天|时)?", normalized)
    if yuan and yuan.group(3) in (None, "月"):
        return float(yuan.group(1)) / 1000, float(yuan.group(2)) / 1000, "CNY"
    single_yuan = re.search(r"(\d{3,6})\s*元\s*/?\s*(月|天|时)?", normalized)
    if single_yuan and single_yuan.group(2) in (None, "月"):
        value = float(single_yuan.group(1)) / 1000
        return value, value, "CNY"
    return None, None, ""
