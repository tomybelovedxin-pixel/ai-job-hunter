"""岗位争取判断：先判断"能不能交付"，再决定用哪条轨道、说什么。

原来的判断只问一句"我以前做过吗"，对不上的岗位直接被丢掉。现在分两步：

1. 交付可行性：把 JD 里的要求/职责拆成任务，逐条问"这条现在处于哪一档能力状态"
   （见 jobagent/capability.py）。有证据的算满，靠 AI 工具补的按补全进度折算，
   客观受限的算 0。整体交付可信度低于 40% 才不发。
2. 轨道选择：
   A 代表本人求职；按真实经历和实物证据说；
   B AI 能力变现；对方买的是自动化/工具/系统交付，就说清交付什么、怎么验收。
   两条轨道可以同时成立，话术的落点不同。

输出里所有结论都带依据（哪条要求、什么证据、什么限制），便于事后复核，
也保证"没做过"不等于"不能争取"，而"AI 理论上能生成代码"也不等于"已验证的交付能力"。
"""

from __future__ import annotations

import re
from typing import Any

from . import capability
from .profile import Profile

# 交付可信度低于这个值就不发这家（口径与 AGENTS.md 的覆盖率 40% 一致）
MIN_DELIVERY_RATIO = 0.4

# JD 里有这些说法，说明对方要买的是交付/服务/工具，轨道 B 成立
SERVICE_SIGNALS = re.compile(
    r"(外包|项目制|按项目|按单|甲方|乙方|供应商|服务商|驻场开发|远程交付|兼职|"
    r"系统交付|软件交付|定制开发|二次开发|自动化改造|流程外包|技术服务|"
    r"按人天|按人月|验收标准|交付物|需求方)", re.I)

# 岗位类型标签（报告和话术都用这一个口径）。顺序 = 从具体到宽泛：
# 先认标题里最具体的那个类型，标题认不出来再看正文，避免"JD 里提了一句大模型"
# 就把一个评测岗或采集岗算成 AI 应用岗。
JOB_TYPES: list[tuple[str, re.Pattern[str]]] = [
    ("采集 / 爬虫", re.compile(r"(采集|爬虫|抓取|数据获取)", re.I)),
    ("数据 / 评测 / 标注", re.compile(r"(标注|评测|数据质量|数据分析|etl|数据清洗|训练数据)", re.I)),
    ("RPA / 流程自动化", re.compile(r"(rpa|流程自动化|影刀|云扩|uipath|自动化流程)", re.I)),
    ("ERP / 业务系统", re.compile(r"(erp|金蝶|用友|odoo|进销存|mes\b|wms)", re.I)),
    ("AI 客服 / 对话系统", re.compile(r"(客服|对话|问答|工单)", re.I)),
    ("AI Agent / 智能体", re.compile(r"(agent|智能体|多智能体|工作流编排|langchain|dify|coze|n8n)", re.I)),
    ("技术方案 / 售前", re.compile(r"(方案|售前|技术文档|需求分析|实施顾问|解决方案)", re.I)),
    ("全栈", re.compile(r"(全栈|前后端|full ?stack)", re.I)),
    ("前端", re.compile(r"(前端|vue|react|小程序|页面开发|html)", re.I)),
    ("后端 / 接口", re.compile(r"(后端|服务端|接口开发|api 开发|fastapi|flask|django|go 开发)", re.I)),
    ("自动化 / 交付", re.compile(r"(自动化|脚本|批处理|定时任务|交付|项目制|python)", re.I)),
    ("AI 应用 / 大模型", re.compile(r"(ai 应用|ai应用|大模型|llm|gpt|prompt|提示词|rag|知识库|ai 工程师)", re.I)),
]


def job_type(job: Any, analysis: dict[str, Any] | None = None) -> str:
    """给岗位贴一个类型标签（报告、话术、回归测试都按它分组）。

    先只看标题：标题写了什么就是什么；标题认不出来才看正文。
    """
    get = (lambda key, default="": job[key] if not isinstance(job, dict) else job.get(key, default))
    title = str(get("title") or "")
    blob = str(get("description") or "")
    for source in (title, f"{title}\n{blob}"):
        for name, pattern in JOB_TYPES:
            if pattern.search(source):
                return name
    return "其他"


def job_tasks(job: Any, analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """把 JD 拆成任务清单：要求/职责句优先，其次正文行，最后退回标题。"""
    get = (lambda key, default="": job[key] if not isinstance(job, dict) else job.get(key, default))
    tasks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in (analysis.get("requirements") or []):
        text = str(item.get("text") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        tasks.append({"text": text, "section": item.get("section") or "要求",
                      "capability": item.get("capability") or "", "grade": item.get("grade") or "D"})
    if not tasks:
        for line in re.split(r"[\n；;。]+", str(get("description") or "")):
            line = line.strip()
            if 6 <= len(line) <= 200 and line not in seen:
                seen.add(line)
                tasks.append({"text": line, "section": "职责", "capability": "", "grade": "D"})
    if not tasks:
        title = str(get("title") or "")
        if title:
            tasks.append({"text": title, "section": "标题", "capability": "", "grade": "D"})
    return tasks[:14]


def assess(job: Any, analysis: dict[str, Any], profile: Profile, *,
           plan: dict[str, Any] | None = None,
           contacted_before: bool = False) -> dict[str, Any]:
    """判断这个岗位现在要不要争取、用哪条轨道、可以拿什么当切入口。"""
    get = (lambda key, default="": job[key] if not isinstance(job, dict) else job.get(key, default))
    title = str(get("title") or "")
    jd_text = str(get("description") or "")
    plan = plan if plan is not None else capability.load_plan()
    checks = analysis.get("checks") or {}

    reasons: list[str] = []
    stops: list[str] = []

    blocked = checks.get("blocked") or []
    if blocked:
        stops.append("命中红线：" + "、".join(blocked))
    salary_note = str((checks.get("salary") or {}).get("note") or "")
    if salary_note and "低于你设定的底线" in salary_note:
        stops.append(salary_note)
    if contacted_before:
        stops.append("这家已经联系过（或用过/被拒过），不重复打扰")

    # ---- 交付可行性：逐条任务判断能力状态 ----
    tasks = job_tasks(job, analysis)
    task_states: list[dict[str, Any]] = []
    for item in tasks:
        state = capability.assess_task(item["text"], profile=profile, plan=plan)
        grade = str(item.get("grade") or "D")
        evidence_value = {"A": 1.0, "B": 0.6}.get(grade, 0.0)
        scoped = capability.in_scope(item["text"])
        deliver = capability.deliverable_value(state["level"], scoped=scoped,
                                               evidence=evidence_value)
        task_states.append({
            **state,
            "grade": grade,
            "section": item["section"],
            "scoped": scoped,
            "delivery": round(deliver, 2),
            "evidence_backed": grade in ("A", "B") and bool(item.get("capability")),
        })

    # 同一项能力在多条要求里出现只算一次（和 analyze 的口径一致）
    deduped: dict[str, dict[str, Any]] = {}
    for state in task_states:
        key = state.get("capability") or state["task"][:24]
        current = deduped.get(key)
        if current is None or state["delivery"] > current["delivery"]:
            deduped[key] = state
    considered = list(deduped.values()) or task_states
    delivery_ratio = (sum(s["delivery"] for s in considered) / len(considered)
                      if considered else 0.0)

    blocked_tasks = [s for s in considered if s["level"] == "暂不可行"]
    reusable = [s for s in considered if s["level"] == "可复用能力"]
    completing = [s for s in considered if s["level"] == "正在补全"]
    evidence_items = [s for s in considered if s["evidence_backed"]]

    if delivery_ratio < MIN_DELIVERY_RATIO:
        stops.append(f"交付可信度只有 {delivery_ratio:.0%}，低于 {MIN_DELIVERY_RATIO:.0%}")

    # ---- 轨道 ----
    service_hit = SERVICE_SIGNALS.search(f"{title}\n{jd_text}")
    track = "A"
    if service_hit:
        track = "B"
        reasons.append(f"对方写的是「{service_hit.group(0)}」，属于买交付/服务，走轨道 B")
    else:
        reasons.append("常规招聘岗位，走轨道 A（代表本人求职，讲真实经历与实物）")
    if analysis.get("matched"):
        reasons.append(f"有 {len(analysis['matched'])} 项 A/B 级证据可以直接引 JD 原话")
    if completing:
        reasons.append(f"有 {len(completing)} 项要求本机没有现成证据，但属可补全范围，"
                       f"对外只能提试做/小样，不能说做过")
    if reusable:
        reasons.append(f"有 {len(reusable)} 项能在 GitHub 找到候选方案（未验证，只作内部可行性判断）")
    if blocked_tasks:
        reasons.append("有 " + str(len(blocked_tasks)) + " 项属客观受限："
                       + "；".join(sorted({s["reason"] for s in blocked_tasks}))[:160])

    # ---- 切入口：最多两个优势点，一条有证据 + 至多一条能做/能顶 ----
    focus: list[dict[str, Any]] = []
    for item in (analysis.get("matched") or []):
        name = str(item.get("capability") or "")
        if name and all(f["capability"] != name for f in focus):
            focus.append({"capability": name, "kind": "evidence",
                          "quote": item.get("text") or "", "grade": item.get("grade") or "",
                          "basis": item.get("basis") or ""})
        if len(focus) >= 1:
            break
    secondary = _secondary_focus(job, analysis)
    if secondary:
        focus.append(secondary)
    if not focus:
        # 一条证据都没有：切成能上手做 + 先给一版验证的说法，不冒充做过
        focus.append({"capability": "", "kind": "ai_assisted",
                      "quote": _first_task_quote(tasks), "grade": "",
                      "basis": "本机没有同类项目的成品证据，只能按试做推进"})

    # 没有证据项、也没有"能做/能顶"的点，说明这条 JD 里找不到能说出口的具体东西：
    # 与其发一段谁都能发的空话，不如不发（用户口径：三样都没有才不发这家）。
    best_delivery = max((s["delivery"] for s in considered), default=0.0)
    if not evidence_items and not secondary and best_delivery < 0.5:
        stops.append("JD 里找不到能说出口的具体点（没有证据项，也没有能上手的活），不发")

    risks = capability.risk_notes(f"{title}\n{jd_text}")
    narrow_scope = bool(risks) and not evidence_items
    if risks:
        reasons.append("涉及" + "、".join(risks) + "，话术只收窄到可验证的一段，不承诺整体承担")
    if not evidence_items and not secondary:
        reasons.append("没有现成证据项，首条按能上手加先做一版验证说，不提做过")

    return {
        "decision": "stop" if stops else "send",
        "stops": stops,
        "track": track,
        "track_label": ("A 代表本人求职" if track == "A" else "B AI 能力变现"),
        "type": job_type(job, analysis),
        "delivery_ratio": round(delivery_ratio, 3),
        "task_states": task_states,
        "considered": considered,
        "evidence_count": len(evidence_items),
        "reusable_count": len(reusable),
        "completing_count": len(completing),
        "blocked_tasks": blocked_tasks,
        "focus": focus[:2],
        "risk_notes": risks,
        "narrow_scope": narrow_scope,
        "reasons": reasons,
    }


def _secondary_focus(job: Any, analysis: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """第二个优势点：只取会做但没做过、或能顶的非技术项，都不许说成做过。

    只在 JD 的要求/职责句子里找，不看整段正文——否则"处理页面改版带来的问题"
    这种顺带一句会被当成"招前端"。
    """
    from .greeter import CAN_DO, NON_TECH_KEYS

    get = (lambda key, default="": job[key] if not isinstance(job, dict) else job.get(key, default))
    texts = [str(item.get("text") or "") for item in ((analysis or {}).get("requirements") or [])]
    blob = "\n".join(texts) if texts else str(get("description") or "")
    title = str(get("title") or "")
    # 按"这条 JD 里哪一项最核心"挑：标题点了的优先，其次看要求句里命中几次。
    # 原来按 CAN_DO 的书写顺序取第一条，导致 ERP 实施岗去讲 Docker 部署。
    best: tuple[int, tuple[str, ...], str] | None = None
    best_score = 0
    for pattern, lines, family in CAN_DO:
        title_hit = bool(pattern.search(title))
        hits = sum(1 for text in texts if pattern.search(text))
        if not title_hit and not hits:
            continue
        score = (3 if title_hit else 0) + hits * 2
        if score > best_score:
            best, best_score = (score, lines, family), score
    if best:
        _score, lines, family = best
        # 引文留空：命中词可能是"学历不限"这种从正则里捡出来的碎片，
        # 拼进消息就是答非所问（2026-10-17 实测踩过"「学历不限…」这类活我能上手做"）
        return {"capability": "", "kind": "can_do", "quote": "",
                "speak": lines[0], "lines": lines, "question_family": family, "grade": "",
                "basis": "按 JD 原话说能做，不编项目经历"}
    if NON_TECH_KEYS.search(f"{get('title')}\n{blob}"):
        return {"capability": "", "kind": "mgmt", "quote": "", "grade": "",
                "basis": "带团队/对接客户这类由本人来顶"}
    return None


def _first_task_quote(tasks: list[dict[str, Any]]) -> str:
    """给"先做一版验证"那层挑一句 JD 原文当落点。

    只挑像工作内容的句子：学历、经验年限、薪资福利这类句子引出来就是答非所问
    （2026-10-17 实测踩过"「学历不限…」这类活我能上手做"）。
    """
    from .analyze import JUNK_ZH, SKILL_HINT
    from .greeter import QUOTE_JUNK

    for item in tasks:
        text = str(item.get("text") or "").strip()
        if not (6 <= len(text) <= 40):
            continue
        if QUOTE_JUNK.search(text) or JUNK_ZH.search(text):
            continue
        if not SKILL_HINT.search(text):
            continue
        if item.get("section") == "要求":
            return text
    # 没有像样的工作内容可引就留空：消息里少一个引文，比引错强
    return ""
