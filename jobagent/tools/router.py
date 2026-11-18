"""按任务选工具：先认出任务要什么能力，再从目录里挑已验证的工具。

选择依据写在返回值里：为什么选它、为什么不用别人、缺什么就没得选。
没通过验证的工具不会进入"可自主调用"的候选。
"""

from __future__ import annotations

import re
from typing import Any

from .catalog import STATES, Catalog
from .registry import REGISTRY

# 意图 → 关键说法
INTENTS: dict[str, list[str]] = {
    "采集岗位": ["搜索岗位", "找岗位", "抓岗位", "采集招聘", "招聘信息", "岗位数据", "job search"],
    "解析岗位": ["解析jd", "岗位要求", "jd 解析", "匹配度", "能不能胜任", "适合我吗", "分析岗位"],
    "简历产出": ["简历", "resume", "cv", "定制简历", "改简历"],
    "求职材料": ["求职信", "自我介绍", "私信", "打招呼", "话术", "面试问题", "面试提纲"],
    "文档校验": ["分页", "排版", "截断", "检查简历", "pdf 检查"],
    "文本质量": ["去ai味", "自然表达", "润色", "ai味", "文风", "文字质量"],
    "开源工具发现": ["开源", "github", "github 上", "有没有工具", "找项目", "集成工具"],
    "定时任务": ["定时", "每天跑", "周期性", "自动跑", "后台跑"],
    "进度记录": ["进度", "跟踪", "记录", "报告", "导出报告"],
}

# 意图 → 能用的工具能力标签
INTENT_CAPABILITIES: dict[str, list[str]] = {
    "采集岗位": ["岗位采集", "数据采集"],
    "解析岗位": ["岗位匹配", "JD 解析"],
    "简历产出": ["简历产出"],
    "求职材料": ["简历产出", "文本质量"],
    "文档校验": ["文档校验", "简历产出"],
    "文本质量": ["文本质量"],
    "开源工具发现": ["开源工具发现"],
    "定时任务": ["定时任务"],
    "进度记录": ["进度记录"],
}

# 兜底链路：某个能力没有已验证工具时，告诉用户还能怎么走
FALLBACKS: dict[str, str] = {
    "采集岗位": "可以改用内置的 tab 取数（job_collect），或把 JD 复制出来走手工导入",
    "解析岗位": "可以用内置规则引擎（jd_analyze），不依赖任何外部仓库",
    "简历产出": "渲染依赖缺失时先出 Markdown，装上 reportlab / python-docx 后再出 DOCX 与 PDF",
    "文本质量": "规则层始终可用（text_quality_scan）；技能包缺失时重新安装本机插件",
    "开源工具发现": "GitHub 匿名限流时设置 GITHUB_TOKEN 再试",
    "定时任务": "没有可调用的调度工具时，用 Windows 计划任务调用 run_once",
    "进度记录": "内置报告生成器始终可用",
}


def detect_intent(task: str) -> tuple[str, list[tuple[str, int]]]:
    lowered = (task or "").lower()
    scores: list[tuple[str, int]] = []
    for intent, phrases in INTENTS.items():
        score = sum(1 for phrase in phrases if phrase.lower() in lowered)
        scores.append((intent, score))
    scores.sort(key=lambda item: -item[1])
    if scores and scores[0][1] > 0:
        return scores[0][0], scores
    return "", scores


def route(task: str, catalog: Catalog | None = None) -> dict[str, Any]:
    intent, scores = detect_intent(task)
    if not intent:
        return {
            "task": task,
            "intent": "",
            "chosen": None,
            "candidates": [],
            "reason": "没识别出任务意图。可以换一种说法，或者直接指定工具名。",
            "fallback": "把任务说得更具体一些，例如「搜索岗位」「分析这条 JD」「生成简历」",
            "scores": scores,
        }

    capabilities = INTENT_CAPABILITIES.get(intent, [])
    candidates: list[dict[str, Any]] = []
    for spec in REGISTRY.values():
        if spec.capability not in capabilities:
            continue
        state, auto_call, verified = "C", spec.auto_call, False
        if catalog is not None:
            tool = catalog.get(spec.name)
            if tool:
                state, auto_call, verified = tool.state, tool.auto_call, tool.verified
        candidates.append({**spec.describe(), "state": state,
                           "state_label": STATES.get(state, state), "verified": verified,
                           "auto_callable": bool(state == "D" and auto_call)})

    def rank(item: dict[str, Any]) -> tuple[int, int, int]:
        # 优先级：已验证可自主调用 > 已验证但需人工 > 装了没验证 > 其余；同档看依赖成本
        tier = 0 if item["auto_callable"] else (1 if item["verified"] else (2 if item["state"] == "C" else 3))
        cost = 1 if "标准库" in item["runtime"] or "内置" in item["kind"] else 2
        return (tier, cost, len(item["name"]))

    candidates.sort(key=rank)
    chosen = next((item for item in candidates if item["auto_callable"]), None)
    if chosen:
        reason = (f"意图「{intent}」→ 能力「{chosen['capability']}」；"
                  f"{chosen['name']} 已通过验证且允许自主调用（{chosen['purpose']}）")
    elif candidates:
        best = candidates[0]
        reason = (f"意图「{intent}」有候选，但没有已验证可自主调用的工具；"
                  f"最接近的是 {best['name']}（{best['state']} {best['state_label']}），"
                  f"先跑 tool verify {best['name']}")
    else:
        reason = f"意图「{intent}」在目录里还没有对应工具"

    return {
        "task": task,
        "intent": intent,
        "chosen": chosen,
        "candidates": candidates,
        "reason": reason,
        "fallback": FALLBACKS.get(intent, "没有对应兜底方案，需要新增工具或模块"),
        "scores": scores,
        "keywords": [word for word in re.split(r"[\s,，。]+", task or "") if word][:12],
    }
