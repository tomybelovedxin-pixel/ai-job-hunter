"""在 GitHub 上按能力域找候选项目，登记成 A 级（已发现、尚未验证）。

这一步只登记搜索结果的公开元数据，不安装、不执行任何仓库代码。
候选人选本身不代表能用，是否可用要等 evaluate / install / verify 走完。
"""

from __future__ import annotations

import time
from typing import Any

from . import github
from .catalog import Catalog, Tool

# 按你要研究的八个方向组织的检索式。stars 阈值用来挡掉玩具项目。
DISCOVERY_QUERIES: list[dict[str, str]] = [
    {"capability": "Agent 编排与工具调用",
     "query": '"tool calling" agent framework language:Python stars:>300'},
    {"capability": "Agent 编排与工具调用",
     "query": 'agent orchestration planning language:Python stars:>500'},
    {"capability": "岗位采集与求职自动化",
     "query": 'job scraper search jobs language:Python stars:>100'},
    {"capability": "岗位采集与求职自动化",
     "query": 'job board aggregation api language:Python stars:>200'},
    {"capability": "简历解析与岗位匹配",
     "query": 'resume parser matching jobs language:Python stars:>100'},
    {"capability": "简历解析与岗位匹配",
     "query": 'resume builder json template language:Python stars:>200'},
    {"capability": "浏览器自动化与表单填写",
     "query": 'browser automation form filling language:Python stars:>300'},
    {"capability": "MCP 与插件框架",
     "query": 'model context protocol server language:Python stars:>100'},
    {"capability": "工作流与任务调度",
     "query": 'task scheduler workflow orchestration language:Python stars:>500'},
    {"capability": "文档与 PDF 生成",
     "query": 'docx pdf generation report language:Python stars:>300'},
    {"capability": "文本质量与表达控制",
     "query": 'writing style linter chinese text language:Python stars:>50'},
    {"capability": "中文文本质量与表达控制",
     "query": 'ai text detection chinese writing stars:>50'},
    {"capability": "测试与运行监控",
     "query": 'pipeline observability retry python stars:>300'},
]


def discover(catalog: Catalog, *, queries: list[str] | None = None, per_page: int = 5,
             pause: float = 7.0, capability: str = "") -> dict[str, Any]:
    """检索并登记候选。匿名状态下 GitHub 搜索接口是 10 次/分钟，所以默认间隔 7 秒。"""
    selected = [{"capability": capability or "自定义", "query": q} for q in queries] \
        if queries else DISCOVERY_QUERIES
    added, refreshed, errors = [], [], []
    for item in selected:
        try:
            repos = github.search_repos(item["query"], per_page=per_page)
        except github.GitHubError as exc:
            errors.append(f"{item['query']} → {exc}")
            continue
        for repo in repos:
            full_name = repo.get("full_name")
            if not full_name:
                continue
            before = catalog.get(full_name)
            tool = Tool(
                name=full_name,
                kind=_guess_kind(repo),
                purpose=(repo.get("description") or "").strip()[:300],
                repo_url=repo.get("html_url") or f"https://github.com/{full_name}",
                docs_url=repo.get("homepage") or "",
                license=((repo.get("license") or {}) or {}).get("spdx_id") or "",
                stars=repo.get("stargazers_count"),
                last_commit=repo.get("pushed_at") or "",
                state="A",
                source=f"GitHub 检索：{item['capability']}",
                notes=f"发现自检索式：{item['query']}",
            )
            catalog.upsert(tool, source=f"discover:{item['capability']}")
            catalog.add_event(full_name, "discover", True,
                              f"{item['capability']}｜stars={repo.get('stargazers_count')}｜"
                              f"license={tool.license}｜pushed={tool.last_commit}")
            (refreshed if before else added).append(full_name)
        time.sleep(pause)
    return {"added": added, "refreshed": refreshed, "errors": errors,
            "queries": len(selected)}


def _guess_kind(repo: dict[str, Any]) -> str:
    name = (repo.get("name") or "").lower()
    description = (repo.get("description") or "").lower()
    topics = [str(t).lower() for t in (repo.get("topics") or [])]
    blob = " ".join([name, description, *topics])
    if "mcp" in blob and ("server" in blob or "model context" in blob):
        return "MCP 服务"
    if "cli" in name or "cli" in topics:
        return "CLI 工具"
    if "skill" in blob and "codex" in blob:
        return "技能/指令包"
    if "api" in blob and repo.get("language") in (None, "") :
        return "外部 API"
    if repo.get("language") in ("Python", "JavaScript", "TypeScript", "Go", "Rust"):
        return "Python 库" if repo.get("language") == "Python" else "独立应用"
    return "仅参考"
