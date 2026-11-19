"""按方向裁剪主简历。

原则：
- 只挑和目标岗位真正相关、而且有证据（等级 A/B）的内容。
- 项目经历按相关度排序，超出的整条丢掉，不做"什么都塞一点"。
- 方向识别是显式规则，判断依据会一并返回，方便你核对。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..evidence import GRADE_ORDER

MASTER_PATH = Path(__file__).resolve().parent.parent.parent / "master_resume.json"

# 方向 → 关键词。命中越多相关度越高；一个都不命中就用 default。
DIRECTIONS: dict[str, list[str]] = {
    "AI Agent 应用": [
        "agent", "智能体", "multi-agent", "工作流", "workflow", "工具调用", "tool calling",
        "mcp", "编排", "上下文",
    ],
    "AI 客服解决方案": [
        "客服", "售后", "工单", "智能客服", "会话", "customer service", "support",
        "服务台", "呼叫中心", "话术", "客诉",
    ],
    "AI 自动化与业务流程": [
        "自动化", "rpa", "流程", "采集", "爬虫", "脚本", "定时", "巡检", "批处理",
        "automation", "workflow automation",
    ],
    "AI 应用开发": [
        "大模型", "llm", "rag", "检索", "应用开发", "python", "prompt", "提示词",
        "大语言模型", "生成式", "ai开发", "ai 开发", "开发工程师", "应用工程师",
    ],
    "Python 自动化与脚本": [
        "python", "脚本", "接口对接", "数据处理", "excel", "运维", "维护", "部署",
        "爬虫", "采集", "自动化", "sdk", "后端",
    ],
    "AI 数据与评测": [
        "标注", "评测", "评估", "数据质量", "rubric", "判题", "训练师", "训练数据",
        "一致性", "抽检", "多模态", "数据集", "数据标注",
    ],
    "内容与图像处理": [
        "图像", "图片", "视频", "素材", "剪辑", "主图", "白底", "水印", "批量处理",
        "文案", "内容", "版式", "图文",
    ],
    "前端与全栈": [
        "前端", "全栈", "vue", "react", "小程序", "页面", "web", "html", "css",
        "javascript", "typescript", "界面",
    ],
}

SKILL_GROUP_ORDER: dict[str, list[str]] = {
    "AI Agent 应用": ["AI 应用", "工程习惯", "后端与数据", "自动化与采集", "文档与表达"],
    "AI 客服解决方案": ["AI 应用", "后端与数据", "自动化与采集", "文档与表达", "工程习惯"],
    "AI 自动化与业务流程": ["自动化与采集", "后端与数据", "AI 应用", "工程习惯", "文档与表达"],
    "AI 应用开发": ["后端与数据", "AI 应用", "工程习惯", "自动化与采集", "文档与表达"],
    "Python 自动化与脚本": ["后端与数据", "自动化与采集", "工程习惯", "AI 应用", "文档与表达"],
    "AI 数据与评测": ["AI 应用", "工程习惯", "后端与数据", "文档与表达", "自动化与采集"],
    "内容与图像处理": ["文档与表达", "AI 应用", "自动化与采集", "后端与数据", "工程习惯"],
    "前端与全栈": ["后端与数据", "AI 应用", "工程习惯", "自动化与采集", "文档与表达"],
}


@dataclass
class ResumeDoc:
    direction: str
    basics: dict[str, Any]
    headline: str
    summary: str
    skill_groups: list[dict[str, str]] = field(default_factory=list)
    projects: list[dict[str, Any]] = field(default_factory=list)
    education: str = ""
    offers: str = ""
    warnings: list[str] = field(default_factory=list)
    used_claims: list[str] = field(default_factory=list)
    relevance: list[tuple[str, float]] = field(default_factory=list)
    others: list[dict[str, str]] = field(default_factory=list)
    target_title: str = ""
    matches: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "basics": self.basics,
            "headline": self.headline,
            "summary": self.summary,
            "skill_groups": self.skill_groups,
            "projects": self.projects,
            "education": self.education,
            "offers": self.offers,
            "warnings": self.warnings,
            "used_claims": self.used_claims,
            "relevance": [{"id": pid, "score": round(score, 1)} for pid, score in self.relevance],
            "others": self.others,
            "target_title": self.target_title,
            "matches": self.matches,
        }


def load_master(path: str | Path | None = None) -> dict[str, Any]:
    target_p = Path(path) if path else MASTER_PATH
    if not target_p.exists():
        try:
            from ..config import load as load_config
            cfg_p = load_config().master_resume_path
            if cfg_p.exists():
                target_p = cfg_p
        except Exception:
            pass
    return json.loads(target_p.read_text(encoding="utf-8"))


def detect_direction(text: str, master: dict[str, Any] | None = None,
                     *, title: str = "") -> tuple[str, list[tuple[str, float]]]:
    """返回 (方向, 各方向得分)。规则显式，便于核对为什么选了这个方向。

    岗位标题里的词权重 ×3：标题是"这个岗要什么人"的直接信号，
    JD 正文里常混着公司介绍和流程描述，按正文判会把方向带偏
    （实测："AI视频生成师"曾被判成"AI 自动化与业务流程"）。
    """
    lowered = (text or "").lower()
    title_lowered = (title or "").lower()
    scores: list[tuple[str, float]] = []
    for direction, keywords in DIRECTIONS.items():
        score = sum(1.0 for keyword in keywords if keyword.lower() in lowered)
        score += 2.0 * sum(1.0 for keyword in keywords if keyword.lower() in title_lowered)
        # 方向名本身出现在标题里，权重更高
        if direction.lower() in title_lowered:
            score += 3.0
        elif direction.lower() in lowered:
            score += 2.0
        scores.append((direction, score))
    scores.sort(key=lambda item: item[1], reverse=True)
    best = scores[0] if scores and scores[0][1] > 0 else ("AI 应用开发", 0.0)
    return best[0], scores


def _verified_index(report: dict[str, Any] | None) -> dict[str, str]:
    if not report:
        return {}
    return {claim.get("id", ""): claim.get("verified_grade", "D")
            for claim in report.get("claims", [])}


def _project_relevance(project: dict[str, Any], direction: str, job_text: str) -> float:
    score = 0.0
    if direction in project.get("directions", []):
        score += 10.0
    elif "default" in project.get("directions", []):
        score += 4.0
    lowered = (job_text or "").lower()
    for token in [project.get("name", ""), *project.get("tech", [])]:
        token = str(token).lower()
        if token and token in lowered:
            score += 3.0
    if project.get("grade") == "A":
        score += 2.0
    return score


def _short(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip(" 　-·—*")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_matches(analysis: dict[str, Any] | None, *, limit: int = 4) -> list[str]:
    """把岗位要求里"确实对得上"的几条，翻成简历开头的一句对应说明。

    只用分析结果里判为具备/部分具备、而且证据等级是 A/B 的条目；
    对不上的、缺证据的一律不写——简历不是许愿池。
    """
    if not analysis:
        return []
    out: list[str] = []
    for item in analysis.get("requirements") or []:
        if str(item.get("grade") or "D") not in ("A", "B"):
            continue
        if str(item.get("verdict") or "") not in ("具备", "部分具备"):
            continue
        text = _short(item.get("text"), 32)
        capability = _short(item.get("capability"), 20)
        basis = _short(item.get("basis"), 44)
        if not text or not capability:
            continue
        line = f"要求「{text}」 → {capability}"
        if basis:
            line += f"：{basis}"
        out.append(line)
        if len(out) >= limit:
            break
    return out


def build_doc(
    master: dict[str, Any],
    *,
    report: dict[str, Any] | None = None,
    job: dict[str, Any] | None = None,
    analysis: dict[str, Any] | None = None,
    direction: str | None = None,
    max_projects: int = 4,
    max_bullets: int = 3,
) -> ResumeDoc:
    job = job or {}
    job_text = " ".join(
        str(job.get(key) or "") for key in ("title", "description", "tags", "company")
    )
    if not direction:
        direction, _ = detect_direction(job_text, master, title=str(job.get("title") or ""))

    grades = _verified_index(report)
    warnings: list[str] = []

    usable_projects: list[dict[str, Any]] = []
    for project in master.get("projects", []):
        claims = project.get("claims") or []
        if report:
            # 严格门禁：提供证据报告时，项目必须有关联证据且至少有一项核定为 A 或 B 级
            if not claims:
                warnings.append(f"项目「{project.get('name')}」未关联任何能力证据，已跳过以防虚构")
                continue
            v_levels = [grades.get(c, "D") for c in claims]
            if any(level in ("A", "B") for level in v_levels):
                usable_projects.append(project)
            else:
                warnings.append(f"项目「{project.get('name')}」核验等级不足（{v_levels}），已跳过")
        else:
            # 未提供报告时（如纯离线预览），项目自身声明等级必须是 A 或 B
            if project.get("grade", "B") in ("A", "B"):
                usable_projects.append(project)

    scored = sorted(
        ((project, _project_relevance(project, direction, job_text)) for project in usable_projects),
        key=lambda item: item[1],
        reverse=True,
    )
    if job_text.strip():
        # 投具体岗位：只留与岗位相关的
        selected = [project for project, score in scored if score > 0][:max_projects]
        if not selected:
            selected = [project for project, _ in scored[:2]]
    else:
        # 通用版：不按岗位筛，A 级优先、其余按事实源顺序，取前几个
        ordered = sorted(scored, key=lambda item: (item[0].get("grade") != "A", -item[1]))
        selected = [project for project, _ in ordered[:max_projects]]

    projects: list[dict[str, Any]] = []
    others: list[dict[str, str]] = []
    used_claims: list[str] = []
    for project in selected:
        bullets = (project.get("bullets", {}) or {}).get(direction) \
            or (project.get("bullets", {}) or {}).get("default") or []
        if not bullets:
            continue
        if report and project.get("claims"):
            computed_grade = max([grades.get(c, "D") for c in project.get("claims", [])],
                                 key=lambda g: GRADE_ORDER.get(g, 1))
        else:
            computed_grade = project.get("grade", "B")

        projects.append({
            "id": project.get("id"),
            "name": project.get("name"),
            "tagline": project.get("tagline", ""),
            "period": project.get("period", ""),
            "role": project.get("role", ""),
            "tech": list(project.get("tech", [])),
            "bullets": list(bullets)[:max_bullets],
            "result": project.get("result", ""),
            "grade": computed_grade,
        })
        used_claims.extend(project.get("claims", []))
        if computed_grade == "B":
            warnings.append(f"{project.get('name')} 的核验等级是 B，只保留有依据的部分")

    # 没进正文的项目，压成一行放在"其他经历"里，不丢信息也不占版面
    for project in usable_projects:
        if any(project.get("id") == item.get("id") for item in projects):
            continue
        if any(project.get("name") == item["name"] for item in others):
            continue
        tagline = project.get("tagline", "") or project.get("role", "")
        if tagline:
            others.append({"name": project.get("name", ""), "line": tagline})

    order = SKILL_GROUP_ORDER.get(direction, list(SKILL_GROUP_ORDER["AI 应用开发"]))
    groups = sorted(
        master.get("skill_groups", []),
        key=lambda group: order.index(group.get("label")) if group.get("label") in order else 99,
    )
    kept_groups = []
    for group in groups:
        claim_ids = group.get("claims", [])
        if report and claim_ids:
            if all(grades.get(claim, "D") not in ("A", "B") for claim in claim_ids):
                warnings.append(f"技能组「{group.get('label')}」无有效 A/B 级证据关联，已省略")
                continue
        kept_groups.append({"label": group.get("label", ""), "items": group.get("items", "")})
        used_claims.extend(claim_ids)

    basics = dict(master.get("basics", {}))
    summary = (master.get("summary", {}) or {}).get(direction) \
        or (master.get("summary", {}) or {}).get("default", "")

    return ResumeDoc(
        direction=direction,
        basics=basics,
        headline=basics.get("headline", ""),
        summary=summary,
        skill_groups=kept_groups,
        projects=projects,
        education=basics.get("education", ""),
        offers=master.get("offers", ""),
        warnings=warnings,
        others=others,
        used_claims=sorted(set(claim for claim in used_claims if claim)),
        relevance=[(project.get("id", ""), score) for project, score in scored],
        target_title=str(job.get("title") or ""),
        matches=build_matches(analysis),
    )


def autofix(doc: ResumeDoc, issues: list[dict[str, Any]], *, max_bullet_chars: int = 130) -> list[str]:
    """按检查结果做机械修正：压短过长的条目、去掉重复条目、砍掉多余项目。"""
    actions: list[str] = []
    kinds = {issue.get("kind") for issue in issues}
    if "bullet_too_long" in kinds or "length" in kinds:
        for project in doc.projects:
            trimmed = []
            for bullet in project["bullets"]:
                if len(bullet) > max_bullet_chars:
                    cut = _cut_at_sentence(bullet, max_bullet_chars)
                    actions.append(f"压短条目：{bullet[:18]}… → {len(cut)} 字")
                    bullet = cut
                trimmed.append(bullet)
            project["bullets"] = trimmed
    if "duplicate" in kinds:
        for project in doc.projects:
            seen: set[str] = set()
            kept = []
            for bullet in project["bullets"]:
                key = re.sub(r"[^\w\u4e00-\u9fff]", "", bullet)[:20]
                if key in seen:
                    actions.append(f"删除重复条目：{bullet[:18]}…")
                    continue
                seen.add(key)
                kept.append(bullet)
            project["bullets"] = kept
    if "too_many_projects" in kinds and len(doc.projects) > 3:
        dropped = doc.projects.pop()
        actions.append(f"删掉相关度最低的项目：{dropped['name']}")
    return actions


def _cut_at_sentence(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    window = text[:limit]
    for mark in ("。", "；", "，", "、"): 
        index = window.rfind(mark)
        if index >= limit * 0.6:
            return window[: index + 1]
    return window.rstrip("，、；") + "。"
