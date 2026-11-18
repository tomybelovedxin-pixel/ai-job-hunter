"""把一句话任务拆成带工具选择的步骤。

模板是显式的：每种意图对应一串步骤，每一步标注用哪个工具、需不需要你确认。
工具缺失时不会硬编一个调用，而是标出"这一步现在做不了"。
"""

from __future__ import annotations

from typing import Any

from .catalog import Catalog
from .router import detect_intent, route

WORKFLOWS: dict[str, list[dict[str, str]]] = {
    "求职全流程": [
        {"goal": "取一批真实岗位并落库", "intent": "采集岗位"},
        {"goal": "拆解每条 JD 的要求，逐条对照能力档案", "intent": "解析岗位"},
        {"goal": "按方向裁剪简历并跑质量检查", "intent": "简历产出"},
        {"goal": "生成自我介绍、求职信、私信与面试提纲", "intent": "求职材料"},
        {"goal": "记录投递进度和状态变化", "intent": "进度记录"},
    ],
    "工具调研": [
        {"goal": "在 GitHub 检索候选项目", "intent": "开源工具发现"},
        {"goal": "评估许可证、维护状态、Windows 兼容与风险", "intent": "开源工具发现"},
        {"goal": "在本机验证能否真正跑起来", "intent": "开源工具发现"},
    ],
}

TRIGGERS = {
    "求职全流程": ("找工作", "投简历", "求职", "应聘", "帮我找"),
    "工具调研": ("调研工具", "找开源", "评估开源", "集成开源"),
}


def plan(task: str, catalog: Catalog | None = None) -> dict[str, Any]:
    workflow = None
    lowered = (task or "").lower()
    for name, keys in TRIGGERS.items():
        if any(key in lowered for key in keys):
            workflow = name
            break
    score = None
    if workflow:
        steps = WORKFLOWS[workflow]
    else:
        intent, _ = detect_intent(task)
        steps = [{"goal": task, "intent": intent or "未识别"}] if intent else []
        if not steps:
            route_result = route(task, catalog)
            return {"task": task, "workflow": "", "steps": [],
                    "reason": route_result["reason"], "fallback": route_result["fallback"]}

    planned: list[dict[str, Any]] = []
    for index, step in enumerate(steps, start=1):
        sub = route(step["goal"], catalog)
        chosen = sub.get("chosen")
        planned.append({
            "step": index,
            "goal": step["goal"],
            "intent": step["intent"],
            "tool": chosen["name"] if chosen else None,
            "tool_state": chosen["state"] if chosen else "无可用工具",
            "entry": chosen["entry"] if chosen else "",
            "needs_confirm": step["intent"] in ("发送消息", "提交申请"),
            "why": sub.get("reason", ""),
            "fallback": sub.get("fallback", ""),
        })
    return {
        "task": task,
        "workflow": workflow or "单步任务",
        "steps": planned,
        "note": "只有 state=D 且 auto_call 的工具会被真正调用；其余步骤会说明缺什么。",
    }
