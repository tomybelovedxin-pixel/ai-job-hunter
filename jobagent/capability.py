"""能力补全与能力分级。

一个岗位值不值得争取，不只看"我以前做过没有"，还要看"现有的 AI 工具链能不能
把它做出来"。这个模块负责把岗位要求翻译成能力状态，状态只有五档，含义写死在这里，
避免"找到了仓库"被当成"已经掌握"，也避免"没做过"被直接当成"做不了"：

  现有能力  本机已经有、而且跑过的模块（对应 capability_grades.json 的 A/B）
  已验证能力 针对某类具体任务实际跑过测试（自检脚本 / 评测用例通过）
  可复用能力 已找到合适的开源方案，尚未部署验证
  正在补全   需要装依赖、写适配代码，或者把几个项目组合起来
  暂不可行   受硬件、权限、平台限制、预算、许可证或合规影响，暂时做不了

能力缺口会落到 `data/capability_plan.json`，里面记录任务、候选项目、下一步和限制。
GitHub 检索只读公开元数据（见 jobagent/tools/github.py），不下载、不执行任何仓库代码。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .analyze import load_grades
from .config import ROOT
from .profile import Profile

PLAN_PATH = ROOT / "data" / "capability_plan.json"

LEVELS = ("现有能力", "已验证能力", "可复用能力", "正在补全", "暂不可行")

# 交付可信度权重：用来判断"这个岗位我们现在接不接得住"。
# 有证据的按 A/B 分档；靠 AI 工具补的按补全进度分档，越靠后越不能对外承诺。
DELIVERY_VALUE = {
    "已验证能力": 1.0,
    "现有能力": 0.7,
    "可复用能力": 0.5,
    "正在补全": 0.35,
    "暂不可行": 0.0,
}

# 客观限制：命中这些说明不是"没做过"，而是当前条件下确实不该接
HARD_LIMITS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(芯片|嵌入式|单片机|plc|fpga|硬件电路|固件|驱动开发)", re.I),
     "涉及硬件与固件，本机没有设备和测试条件"),
    (re.compile(r"(涉密|等保|渗透测试|安全加固|webshell|应急响应|驻场值守)", re.I),
     "涉及安全合规与驻场责任，个人身份无法承担"),
    (re.compile(r"(大规模生产系统|线上核心系统|高并发|双十一|千万级|亿级|7\*24)", re.I),
     "线上核心系统的故障责任重大，只能承担其中可验证的模块，不能整体承诺"),
    (re.compile(r"(生物|医疗诊断|临床|金融风控模型|征信|自动驾驶|无人车|航天)", re.I),
     "属强监管或高风险领域，需要资质与专业背景"),
]

# 需要收窄承诺范围的信号（不阻止争取，但话术里不能承诺"独立负责全部"）
RISK_FLAGS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(大型|生产环境|线上|核心|关键业务|主链路)", re.I), "涉及线上生产系统"),
    (re.compile(r"(安全|权限|加密|合规|审计|隐私|个人信息)", re.I), "涉及安全与合规要求"),
    (re.compile(r"(架构|技术选型|从零搭建平台|平台化|中台)", re.I), "涉及架构与技术选型"),
    (re.compile(r"(团队管理|带团队|管理岗|技术负责人|项目负责人)", re.I), "涉及团队与管理责任"),
    (re.compile(r"(百万级|千万级|大数据量|高并|海量)", re.I), "涉及规模与性能压力"),
]


# ---------------------------------------------------------------------------
# 能力档案：本机已有的东西（唯一事实源是 capability_grades.json）
# ---------------------------------------------------------------------------


def known_capabilities() -> dict[str, dict[str, Any]]:
    """本机能力名 → {level, grade, basis, limit, claims}。"""
    grades = load_grades().get("capabilities", {})
    result: dict[str, dict[str, Any]] = {}
    for name, entry in grades.items():
        grade = str(entry.get("grade") or "D")
        if grade == "A":
            level = "已验证能力"
        elif grade == "B":
            level = "现有能力"
        else:
            # C/D 不是没有能力，而是"还没到能对外说"的状态
            level = "正在补全" if grade == "C" else "暂不可行"
        result[name] = {
            "level": level,
            "grade": grade,
            "basis": entry.get("basis", ""),
            "limit": entry.get("limit", ""),
            "claims": list(entry.get("claims") or []),
        }
    return result


def _capability_for_task(task: str, profile: Profile) -> str:
    """一条任务描述命中哪个本机能力（按关键词命中数取最高）。"""
    lowered = (task or "").lower()
    best, best_hits = "", 0
    for cap in profile.capabilities:
        hits = sum(1 for kw in cap.keywords if kw and str(kw).lower() in lowered)
        if hits > best_hits:
            best, best_hits = cap.name, hits
    return best if best_hits else ""


def in_scope(task: str) -> bool:
    """这条任务是否落在"会做但没做过 / 我本人能顶"的范围里（CAN_DO 与 MGMT 口径）。

    这类任务对外只能说"能做/能顶"，不能说"做过"；内部判断交付可行性时按
    NEED_VERIFY_VALUE 记分，表示"先做一版就能验证"。
    """
    from .greeter import CAN_DO, NON_TECH_KEYS

    text = task or ""
    if NON_TECH_KEYS.search(text):
        return True
    return any(pattern.search(text) for pattern, _lines, _family in CAN_DO)


# "会做但没做过"的交付分：低于有证据的项，高于"还没有任何方案"的项
NEED_VERIFY_VALUE = 0.45


def deliverable_value(level: str, *, scoped: bool = False, evidence: float = 0.0) -> float:
    """某条任务的可交付权重。

    evidence 是分析给出的证据分（A=1.0 / B=0.6），scoped 表示落在"能做/能顶"范围。
    取三者最大值：有证据按证据，没证据但会做的按可验证，剩下的按能力状态档位。
    """
    value = DELIVERY_VALUE.get(level, 0.0)
    if scoped and level in ("正在补全", "可复用能力"):
        value = max(value, NEED_VERIFY_VALUE)
    return max(value, evidence)


def limits_of(task: str) -> str:
    """这条任务有没有客观限制，有就返回原因。"""
    for pattern, reason in HARD_LIMITS:
        if pattern.search(task or ""):
            return reason
    return ""


def risk_notes(text: str) -> list[str]:
    """这段话里有哪些"不能承诺整体承担"的信号。"""
    notes: list[str] = []
    for pattern, note in RISK_FLAGS:
        if pattern.search(text or "") and note not in notes:
            notes.append(note)
    return notes


# ---------------------------------------------------------------------------
# 能力补全计划：data/capability_plan.json
# ---------------------------------------------------------------------------


def load_plan(path: str | Path = PLAN_PATH) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {"version": "1.0", "updated_at": "", "gaps": {}}


def save_plan(plan: dict[str, Any], path: str | Path = PLAN_PATH) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    plan = dict(plan)
    plan["updated_at"] = time.strftime("%Y-%m-%d %H:%M")
    target.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)


def plan_key(task: str) -> str:
    """把任务描述压成一个稳定的键，方便同一类缺口复用结论。"""
    lowered = re.sub(r"\s+", "", task or "").lower()
    return lowered[:40]


def upsert_gap(task: str, *, level: str, candidates: list[dict[str, Any]] | None = None,
               next_step: str = "", notes: str = "", plan: dict[str, Any] | None = None
               ) -> dict[str, Any]:
    """登记/更新一条能力缺口。不覆盖已经验证过的结论。"""
    if level not in LEVELS:
        raise ValueError(f"未知能力状态 {level}")
    plan = plan or load_plan()
    gaps = plan.setdefault("gaps", {})
    key = plan_key(task)
    entry = gaps.get(key) or {}
    if entry.get("verified"):
        # 已经验证过的结论不因为一次新检索就降级
        return entry
    entry.update({
        "task": task,
        "level": level,
        "candidates": candidates if candidates is not None else entry.get("candidates", []),
        "next_step": next_step or entry.get("next_step", ""),
        "notes": notes or entry.get("notes", ""),
        "verified": bool(entry.get("verified")),
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
    })
    gaps[key] = entry
    return entry


def catalog_candidates(task: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """从本机工具目录里找和这条任务相关的候选（已发现/已评估过的开源项目）。"""
    db = ROOT / "data" / "jobs.sqlite"
    if not db.exists():
        return []
    import sqlite3

    words = [w for w in re.split(r"[\s,，。；;、/]+", task or "") if len(w) >= 2]
    if not words:
        return []
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT name, kind, purpose, license, stars, state, repo_url "
            "FROM tools ORDER BY CASE state WHEN 'D' THEN 0 WHEN 'C' THEN 1 "
            "WHEN 'B' THEN 2 WHEN 'A' THEN 3 ELSE 4 END, stars DESC LIMIT 200"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    hits: list[dict[str, Any]] = []
    for row in rows:
        blob = f"{row['name']} {row['purpose'] or ''}".lower()
        score = sum(1 for word in words if word.lower() in blob)
        if score:
            hits.append({**dict(row), "match": score})
    hits.sort(key=lambda item: (-item["match"], -(item["stars"] or 0)))
    return hits[:limit]


# 检索词映射：GitHub 的搜索接口对中文命中很差，缺口写在中文里就先换成英文关键词
TERM_MAP: dict[str, str] = {
    "订单": "order", "库存": "inventory", "对账": "reconciliation", "报表": "report",
    "财务": "finance", "数据库": "database", "数据": "data", "清洗": "cleaning",
    "采集": "scraper", "爬虫": "crawler", "抓取": "scraping", "浏览器": "browser",
    "自动化": "automation", "流程": "workflow", "部署": "deploy", "容器": "docker",
    "运维": "ops", "服务器": "server", "文档": "document", "方案": "solution",
    "评测": "evaluation", "标注": "annotation", "客服": "customer service",
    "知识库": "knowledge base", "检索": "retrieval", "问答": "qa", "大模型": "llm",
    "接口": "api", "前端": "frontend", "页面": "frontend", "测试": "testing",
    "权限": "auth", "日志": "logging", "监控": "monitoring", "导入": "import",
    "导出": "export", "同步": "sync", "表格": "spreadsheet", "指标": "metrics",
}


def build_query(task: str) -> str:
    """把中文缺口描述转成 GitHub 能搜到的检索式（英文关键词 + language:Python）。"""
    words: list[str] = re.findall(r"[A-Za-z][A-Za-z0-9\-\.]+", task or "")
    terms: list[str] = []
    for key, english in TERM_MAP.items():
        if key in (task or "") and english not in terms:
            terms.append(english)
    if not words:
        for chunk in re.split(r"[\s,，。；;、/]+", task or ""):
            chunk = chunk.strip()
            if chunk and chunk not in terms:
                terms.append(chunk)
    picked = (words + terms)[:4]
    return " ".join(picked) + " language:Python"


def github_candidates(task: str, *, per_page: int = 5, save: bool = True,
                      pause: float = 0.0) -> dict[str, Any]:
    """到 GitHub 上按缺口检索候选项目，登记进能力计划与工具目录。

    只读公开元数据：不下载仓库、不执行脚本、不安装依赖。
    匿名接口每小时 60 次，批量检索时把 pause 设大一点。
    """
    from .tools import github

    query = build_query(task)
    result: dict[str, Any] = {"task": task, "query": query, "candidates": [], "error": ""}
    try:
        repos = github.search_repos(query, per_page=per_page)
    except github.GitHubError as exc:
        result["error"] = str(exc)
        return result
    if not repos:
        # 词太多会搜不到东西：退一步只用前两个词再试一次
        short = " ".join(query.split()[:2]) + " language:Python"
        if short != query:
            result["query"] = short
            try:
                repos = github.search_repos(short, per_page=per_page)
            except github.GitHubError as exc:
                result["error"] = str(exc)
                return result
    seen: set[str] = set()
    for repo in repos:
        full_name = repo.get("full_name") or ""
        if not full_name or full_name in seen:
            continue
        blob = f"{full_name} {repo.get('description') or ''}".lower()
        # 教程/课程/练习本不是可复用的方案（2026-10-17 实测搜到过 nanodegree 那类）
        if re.search(r"(nanodegree|tutorial|course|learning|notes|练习|教程|笔记|"
                     r"awesome-|roadmap|demo[-_]?only|homework)", blob):
            continue
        seen.add(full_name)
        result["candidates"].append({
            "repo": full_name,
            "stars": repo.get("stargazers_count"),
            "license": ((repo.get("license") or {}) or {}).get("spdx_id") or "未声明",
            "pushed_at": repo.get("pushed_at") or "",
            "why": (repo.get("description") or "").strip()[:160],
            "url": repo.get("html_url") or f"https://github.com/{full_name}",
        })
    if pause:
        time.sleep(pause)
    if save and result["candidates"]:
        plan = load_plan()
        upsert_gap(
            task, level="可复用能力", candidates=result["candidates"],
            next_step="先读官方 README 与安装文档，在本机跑最小验证，通过后再谈能不能接这个岗位",
            notes=f"GitHub 检索：{query}",
            plan=plan,
        )
        save_plan(plan)
        _register_in_catalog(result["candidates"], task=task, query=query)
    return result


def _register_in_catalog(candidates: list[dict[str, Any]], *, task: str, query: str) -> int:
    """把候选项目登记进本机工具目录（状态 A：已发现，尚未验证）。

    只登记公开元数据，不安装、不执行。状态定义见 jobagent/tools/catalog.py：
    A 已发现 → B 已评估 → C 已安装 → D 已验证，任何一步都要另外跑命令，不会自动跳。
    """
    import sqlite3

    from .tools.catalog import Catalog, Tool

    db = ROOT / "data" / "jobs.sqlite"
    if not db.exists():
        return 0
    count = 0
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        catalog = Catalog(con)
        for item in candidates:
            catalog.upsert(Tool(
                name=item["repo"],
                kind="仅参考",
                purpose=item.get("why") or "",
                repo_url=item.get("url") or "",
                license=item.get("license") or "",
                stars=item.get("stars"),
                last_commit=item.get("pushed_at") or "",
                state="A",
                source=f"能力补全：{task[:24]}",
                notes=f"发现自 GitHub 检索：{query}",
            ), source=f"能力补全：{task[:24]}")
            catalog.add_event(item["repo"], "discover", True,
                              f"能力缺口检索｜{query}｜stars={item.get('stars')}｜"
                              f"license={item.get('license')}")
            count += 1
    finally:
        con.close()
    return count


# ---------------------------------------------------------------------------
# 对外入口：把一串任务描述 → 能力状态
# ---------------------------------------------------------------------------


def assess_task(task: str, *, profile: Profile, plan: dict[str, Any] | None = None
                ) -> dict[str, Any]:
    """判断一条岗位任务现在处于哪一档，并给出依据。"""
    plan = plan or load_plan()
    limit = limits_of(task)
    if limit:
        return {"task": task, "level": "暂不可行", "reason": limit, "capability": "",
                "evidence": "", "next_step": "", "candidates": []}

    capacity = _capability_for_task(task, profile)
    if capacity:
        info = known_capabilities().get(capacity, {})
        if info.get("grade") in ("A", "B"):
            return {"task": task, "level": info.get("level", "现有能力"), "capability": capacity,
                    "reason": info.get("basis", ""), "evidence": info.get("basis", ""),
                    "limit": info.get("limit", ""), "next_step": "", "candidates": []}

    entry = (plan.get("gaps") or {}).get(plan_key(task))
    if entry:
        level = entry.get("level") or "正在补全"
        if entry.get("verified"):
            level = "已验证能力"
        return {"task": task, "level": level if level in LEVELS else "正在补全",
                "capability": capacity, "reason": entry.get("notes", ""),
                "limit": entry.get("limitation", ""), "next_step": entry.get("next_step", ""),
                "candidates": entry.get("candidates", [])}

    catalog = catalog_candidates(task)
    if catalog:
        return {"task": task, "level": "可复用能力", "capability": capacity,
                "reason": f"工具目录里有候选：{catalog[0]['name']}",
                "limit": "", "next_step": "跑一遍最小验证再判断能不能接",
                "candidates": catalog}

    if capacity:  # C/D 级：本机有相关内容但没落地
        return {"task": task, "level": "正在补全", "capability": capacity,
                "reason": "本机只有部分相关代码，还没有针对这类任务的验证",
                "limit": "", "next_step": "先拆成可验证的小任务，用现有工具跑一版",
                "candidates": []}

    return {"task": task, "level": "正在补全", "capability": "",
            "reason": "本机没有对应的现成模块",
            "next_step": "到 GitHub 检索候选项目（python -m jobagent tool discover 或 "
                         "jobagent.capability.github_candidates）",
            "candidates": []}


def assess_tasks(tasks: list[str], *, profile: Profile,
                 plan: dict[str, Any] | None = None) -> dict[str, Any]:
    """一组任务的整体判断：交付可信度、可复用方案、客观受限的部分。"""
    plan = plan if plan is not None else load_plan()
    items = [assess_task(task, profile=profile, plan=plan) for task in tasks if task]
    if not items:
        return {"items": [], "delivery_ratio": 0.0, "levels": {}, "blocked": [],
                "reusable": [], "notes": ["没有可拆解的任务描述"]}
    levels: dict[str, int] = {}
    for item in items:
        levels[item["level"]] = levels.get(item["level"], 0) + 1
    ratio = sum(DELIVERY_VALUE.get(item["level"], 0.0) for item in items) / len(items)
    return {
        "items": items,
        "delivery_ratio": round(ratio, 3),
        "levels": levels,
        "blocked": [item for item in items if item["level"] == "暂不可行"],
        "reusable": [item for item in items if item["level"] == "可复用能力"],
        "notes": [f"{item['task'][:40]} → {item['level']}" for item in items],
    }


def delivery_value_for(level: str) -> float:
    return DELIVERY_VALUE.get(level, 0.0)
