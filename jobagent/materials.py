"""求职材料生成：自我介绍、求职信、私信与回复建议、面试提纲、技能缺口清单。

所有材料只从两处取材：岗位分析结果 + 主简历里等级为 A/B 的事实。
缺什么就写缺什么，不编造工作经历，也不把没做的事写成做过。
"""

from __future__ import annotations

from typing import Any

from .llm import LLM
from .resume.tailor import load_master
from .textquality import clean, scan


def _usable_projects(master: dict[str, Any], direction: str) -> list[dict[str, Any]]:
    projects = [p for p in master.get("projects", []) if p.get("grade") in ("A", "B")]
    matched = [p for p in projects if direction in p.get("directions", [])]
    return (matched or projects)[:3]


def _first_bullet(project: dict[str, Any], direction: str) -> str:
    bullets = (project.get("bullets", {}) or {}).get(direction) \
        or (project.get("bullets", {}) or {}).get("default") or []
    return bullets[0] if bullets else ""


def _title_of(job: Any) -> str:
    return job["title"] if not isinstance(job, dict) else (job.get("title") or "")


def _quote(text: str, limit: int = 34) -> str:
    """引用 JD 原话时在标点处截断，别把句子拦腰砍断。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    window = text[:limit]
    for mark in ("。", "；", "，", "、", ";", ",", "："):
        index = window.rfind(mark)
        if index >= limit * 0.5:
            return window[: index + 1].rstrip("，、；：,; ")
    return window + "…"


def build_self_intro(job: Any, analysis: dict[str, Any], *,
                     master: dict[str, Any] | None = None) -> str:
    master = master or load_master()
    direction = analysis.get("checks", {}).get("direction", {}).get("value") or "AI 应用开发"
    projects = _usable_projects(master, direction)
    basics = master.get("basics", {})
    parts: list[str] = []
    parts.append(
        f"我是{basics.get('name', '')}，人在{basics.get('city', '')}，"
        f"做{direction}方向，主力语言是 Python。"
    )
    if projects:
        project = projects[0]
        parts.append(f"最近在做{project['name']}，{_first_bullet(project, direction).rstrip('。')}。")
    strengths = analysis.get("strengths", [])[:1]
    if strengths:
        parts.append(f"和你们这条 JD 对得上的是 {strengths[0].rstrip('。')}。")
    gaps = analysis.get("gaps", [])[:1]
    if gaps:
        parts.append(f"对不上的是：{gaps[0].rstrip('。')}。")
    if analysis.get("questions"):
        parts.append(analysis["questions"][0])
    return clean("\n\n".join(parts))


def build_cover_letter(job: Any, analysis: dict[str, Any], *,
                       master: dict[str, Any] | None = None) -> str:
    master = master or load_master()
    direction = analysis.get("checks", {}).get("direction", {}).get("value") or "AI 应用开发"
    projects = _usable_projects(master, direction)
    requirements = [item for item in analysis.get("requirements", [])
                    if item.get("grade") in ("A", "B")]
    lines: list[str] = [f"您好，应聘{_title_of(job)}这个岗位。"]
    if requirements:
        item = requirements[0]
        basis = (item.get("basis") or "").rstrip("。")
        lines.append(
            f"你们的 JD 里提到「{_quote(item['text'])}」，这块我做过：{basis}。"
        )
    if analysis.get("risks"):
        boundary = next((risk for risk in analysis["risks"] if "边界" in risk), "")
        if boundary:
            lines.append(f"也说清楚边界：{boundary.split('：', 1)[-1].rstrip('。')}。")
    if projects:
        project = projects[0]
        lines.append(f"拿{project['name']}举例：{_first_bullet(project, direction)}")
    gaps = [gap for gap in analysis.get("gaps", []) if "红线" not in gap]
    if gaps:
        lines.append(f"要提前说明的是，{gaps[0].rstrip('。')}，这点我不硬撑。")
    lines.append(
        "我可以提供代码仓库、测试与检查记录，也接受先按你们的验收标准做一个小样例，"
        "通过之后再往下谈。如果方便，想先确认一下岗位的验收口径和协作方式。"
    )
    return clean("\n\n".join(lines))


def build_chat_messages(job: Any, analysis: dict[str, Any], *,
                        master: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """打招呼与常见追问的回复建议。语气照着你自己手写的那批草稿来。"""
    master = master or load_master()
    direction = analysis.get("checks", {}).get("direction", {}).get("value") or "AI 应用开发"
    projects = _usable_projects(master, direction)
    project_name = projects[0]["name"] if projects else "手上的项目"
    strength = (analysis.get("strengths") or [""])[0]
    gap = next((g for g in analysis.get("gaps", []) if "红线" not in g), "")
    question = (analysis.get("questions") or ["这个岗位是按项目结还是长期坐班？"])[0]
    messages = [
        {"title": "打招呼（第一句）",
         "text": clean(
             f"您好，看到{_title_of(job)}这个岗位。"
             f"{strength.rstrip('。') if strength else '你们的 JD 和我在做的东西方向一致'}，"
             f"我最近在做的{project_name}正好是这一条。"
             + (f"想先问一句，{question}" if question else ""))},
        {"title": "对方问「你有几年企业经验」",
         "text": clean(
             "我没有企业任职年限，这一点直接说明。我的经验来自自己发起并做完整的项目："
             f"{strength.rstrip('。') if strength else '需求、实现、测试和验收都是我自己收尾的'}。"
             "如果你们介意年限，可以先按一个小任务的实际交付来判断。")},
        {"title": "对方问「期望薪资」",
         "text": clean(
             "我按岗位的实际内容来谈，不先报一个虚高的数。方便的话想先知道这个岗位的预算区间"
             "和结算方式（按月、按项目还是按天），我按你们的区间给一个实数。")},
        {"title": "对方问「能不能立刻上手」",
         "text": clean(
             "能上手的是需求拆解、接口对接、数据落库和自动化脚本这几类，"
             + (f"暂时不确定的是{gap.rstrip('。')}，" if gap else "暂时不确定的我会提前说清楚，")
             + "需要现学的地方我会先做一个小样例验证，再决定要不要接。")},
        {"title": "对方要看作品",
         "text": clean(
             "可以，我提供代码仓库和测试记录，也能当面演示。其中和这个岗位最相关的是"
             + (f"{projects[0]['name']}这个项目，{_first_bullet(projects[0], direction)}"
                if projects else "岗位匹配与材料生成这条流水线"))},
    ]
    return messages


def build_interview_guide(job: Any, analysis: dict[str, Any], *,
                          master: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """按岗位要求出问题，并配一段基于真实项目的回答提纲。"""
    master = master or load_master()
    direction = analysis.get("checks", {}).get("direction", {}).get("value") or "AI 应用开发"
    projects = _usable_projects(master, direction)
    guide: list[dict[str, str]] = []
    for item in analysis.get("requirements", [])[:8]:
        if item.get("grade") not in ("A", "B"):
            continue
        outline: list[str] = []
        if projects:
            project = projects[0]
            outline.append(f"背景：{project.get('name')}——{_first_bullet(project, direction)}")
            default_bullets = (project.get("bullets", {}) or {}).get("default", [])
            if len(default_bullets) > 1:
                outline.append(f"做法：{default_bullets[1]}")
            outline.append(f"证据：{item.get('basis') or '本机可现场演示'}")
        outline.append("被追问细节时按实际做过的部分回答，没验证过的直接说明没验证")
        guide.append({
            "question": f"关于「{_quote(item['text'], 44)}」，你具体做过什么？",
            "outline": "\n".join(f"{i + 1}. {line}" for i, line in enumerate(outline)),
            "grade": item.get("grade", ""),
        })
    for item in analysis.get("missing", [])[:3]:
        guide.append({
            "question": f"我们要求「{_quote(item['text'], 36)}」，你这块是什么情况？",
            "outline": "1. 如实说明目前没有做过的证据\n2. 说明你判断自己能多快补上，依据是什么\n"
                       "3. 提出可以先做一个小样例来验证",
            "grade": item.get("grade", ""),
        })
    return guide


def build_gap_plan(analysis: dict[str, Any]) -> list[dict[str, str]]:
    """把缺口翻译成可以动手补的清单。"""
    plan: list[dict[str, str]] = []
    for item in analysis.get("missing", [])[:6]:
        capability = item.get("capability") or "未归类"
        plan.append({
            "gap": item.get("text", "")[:60],
            "capability": capability,
            "action": f"围绕「{capability}」做一个最小可运行样例，跑出结果并留下测试或运行记录；"
                      f"等级从 D 升到 A 之后，这条要求才写进简历",
        })
    for gap in analysis.get("gaps", [])[:4]:
        if gap.startswith(("岗位要求", "学历", "薪资", "地点", "没有企业")):
            plan.append({"gap": gap, "capability": "客观条件",
                         "action": "不是靠补技能能解决的，直接在沟通里说明，或用项目证据替换"})
    return plan


def build_all(job: Any, analysis: dict[str, Any], *, master: dict[str, Any] | None = None,
              llm: LLM | None = None) -> dict[str, Any]:
    """生成一整套材料。给了 llm 就用它润色，失败自动退回原文。"""
    master = master or load_master()
    materials: dict[str, Any] = {
        "self_intro": build_self_intro(job, analysis, master=master),
        "cover_letter": build_cover_letter(job, analysis, master=master),
        "chat_messages": build_chat_messages(job, analysis, master=master),
        "interview": build_interview_guide(job, analysis, master=master),
        "gap_plan": build_gap_plan(analysis),
    }
    polished: dict[str, str] = {}
    if llm is not None:
        for key, register in (("self_intro", "求职自我介绍"), ("cover_letter", "求职信")):
            result = llm.polish(text=materials[key], register=register)
            if result.ok and result.text:
                polished[key] = result.text
    for key, value in polished.items():
        if len(scan(value)["issues"]) <= len(scan(materials[key])["issues"]):
            materials[key] = value
    materials["quality"] = {
        "self_intro": scan(materials["self_intro"]),
        "cover_letter": scan(materials["cover_letter"]),
        "polished_by_model": sorted(polished),
    }
    return materials
