"""评估候选仓库：许可证、维护情况、依赖、Windows 兼容、安全风险。

全部只读：读公开元数据、文件树和少量源码文本，不下载整仓、不执行任何脚本。
结论写成证据行，最终状态由 evaluate 决定（B 可评估后可用 / E 有风险）。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import github
from .catalog import Catalog

# 宽松许可：可以直接集成进项目
PERMISSIVE = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "MPL-2.0",
              "Unlicense", "CC0-1.0", "Python-2.0", "PSF-2.0", "0BSD", "Zlib"}
# 有传染性或商用限制：可以用，但要单独说明
CAUTION = {"GPL-2.0", "GPL-3.0", "AGPL-3.0", "LGPL-2.1", "LGPL-3.0", "SSPL-1.0",
           "BUSL-1.1", "CC-BY-NC-4.0", "Elastic-2.0"}

POSIX_ONLY = (
    "os.fork", "fcntl.", "pty.spawn", "signal.sigkill", "os.setsid", "/bin/sh",
    "apt-get", "brew install", "chmod +x", "#!/bin/sh", "#!/usr/bin/env bash",
    "crontab", "resource.setrlimit",
)
WINDOWS_HINTS = ("windows", "win32", "msvcrt", "pywin32", "winreg", ".exe")

RISKY = {
    "远程脚本执行": (r"curl\s+[^\n|]+\|\s*(bash|sh)", r"iwr\s+[^\n|]+\|\s*iex",
                     r"wget\s+[^\n|]+\|\s*(bash|sh)"),
    "动态执行": (r"\beval\s*\(", r"\bexec\s*\(", r"\bcompile\s*\(", r"base64\s+-d"),
    "凭据读取": (r"\.ssh/id_rsa", r"\.aws/credentials", r"\.netrc", r"keyring\.",
                 r"Login Data", r"Cookies\b", r"password", r"api[_-]?key\s*=\s*['\"]"),
    "可疑外传": (r"requests\.post\(\s*['\"]http", r"webhook", r"pastebin", r"telegram"),
    "提权或关闭防护": (r"Set-MpPreference", r"DisableRealtimeMonitoring", r"sudo\s+rm",
                       r"reg\s+add\s+HKLM", r"netsh\s+advfirewall\s+set"),
}

# 使用条款与合规风险：这类信号不代表代码有问题，但接入前必须评估能不能用
TERMS_RISK = {
    "linkedin": "抓取 LinkedIn 违反其使用条款，且有反爬与封号风险",
    "indeed": "Indeed 明确禁止未授权的自动化抓取",
    "glassdoor": "Glassdoor 使用条款限制自动化访问",
    "ziprecruiter": "平台条款限制自动化抓取",
    "captcha solving": "涉及验证码破解，属于绕过平台防护的范畴",
    "bypass captcha": "明确写着绕过验证码，属于绕过平台防护",
    "bypass cloudflare": "明确写着绕过平台防护，需要在合规上逐条确认",
    "rotating prox": "依赖轮换代理规避访问限制，合规与稳定性都不确定",
    "residential prox": "依赖住宅代理规避访问限制，合规风险高",
    "anti-bot": "明确针对平台反爬机制，接入前必须评估合法性",
    "未授权抓取": "文档自述未授权抓取，不能直接用在招聘平台上",
}

# 这些关键词只在线性文本里出现，容易误伤（例如 agent 框架里的 'llm proxy'），
# 所以只在上下文同时出现合规词时才计入
TERMS_CONTEXT = re.compile(r"(scrap|spider|crawl|爬虫|采集|抓取)", re.I)

DEP_FILES = ("pyproject.toml", "setup.py", "requirements.txt", "package.json",
             "Cargo.toml", "go.mod", "environment.yml")


@dataclass
class Evaluation:
    full_name: str
    license: str = ""
    license_ok: bool | None = None
    stars: int = 0
    archived: bool = False
    last_commit: str = ""
    days_since_commit: int | None = None
    maintain_score: int = 0
    windows: str = "未知"
    windows_evidence: list[str] = field(default_factory=list)
    install: list[str] = field(default_factory=list)
    dependency_files: list[str] = field(default_factory=list)
    has_tests: bool = False
    has_ci: bool = False
    size_kb: int = 0
    risks: list[dict[str, str]] = field(default_factory=list)
    risk_level: str = "低"
    verdict: str = ""
    recommended_state: str = "B"
    integration_kind: str = "Python 库"
    reasons: list[str] = field(default_factory=list)
    calls: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def evaluate(catalog: Catalog, full_name: str, *, allow_e_files: bool = True) -> Evaluation:
    result = Evaluation(full_name=full_name)
    try:
        meta = github.repo(full_name)
        result.calls += 1
    except github.GitHubError as exc:
        catalog.add_event(full_name, "evaluate", False, f"取元数据失败：{exc}")
        result.verdict = f"无法评估：{exc}"
        result.recommended_state = "A"
        return result

    # license 可能是 null、空对象，或没有 spdx_id（NOASSERTION），三种都要能过
    license_meta = meta.get("license") or {}
    result.license = (license_meta.get("spdx_id") or license_meta.get("name")
                      or "未声明")
    if result.license == "NOASSERTION":
        result.license = "未声明（NOASSERTION）"
    result.stars = int(meta.get("stargazers_count") or 0)
    result.archived = bool(meta.get("archived"))
    result.last_commit = meta.get("pushed_at") or ""
    result.size_kb = int(meta.get("size") or 0)
    result.days_since_commit = _days_since(result.last_commit)
    result.install = [x for x in (meta.get("topics") or []) if isinstance(x, str)][:6]

    tree = github.tree(full_name)
    result.calls += 1
    paths = [item.get("path", "") for item in tree]
    result.has_tests = any(p.startswith(("tests/", "test/")) or p.endswith("_test.py")
                           for p in paths)
    result.has_ci = any(p.startswith(".github/workflows/") for p in paths)
    result.dependency_files = [p for p in paths if p.split("/")[-1] in DEP_FILES][:4]

    if result.license in PERMISSIVE:
        result.license_ok = True
        result.reasons.append(f"许可证 {result.license} 属于宽松许可，可直接集成")
    elif result.license in CAUTION:
        result.license_ok = False
        result.reasons.append(f"许可证 {result.license} 有传染性或商用限制，只能作为参考或单独评估")
    elif result.license in ("未声明", "", None, "NOASSERTION"):
        # 有些仓库包含多个子项目，GitHub 识别不出统一许可证；先看有没有 LICENSE 文件
        has_license_file = any(item.get("path", "").lower().startswith(("license", "copying"))
                               for item in (tree or []))
        result.license_ok = None
        result.reasons.append(
            "许可证无法自动判定（仓库里有多个子项目或许可证文件），需要人工打开 LICENSE 确认"
            if has_license_file else "许可证未声明，默认按不可直接复用来处理"
        )

    if result.days_since_commit is not None:
        if result.days_since_commit <= 90:
            result.maintain_score = 2
            result.reasons.append(f"最近 {result.days_since_commit} 天内有提交，维护活跃")
        elif result.days_since_commit <= 365:
            result.maintain_score = 1
            result.reasons.append(f"最近一次提交在 {result.days_since_commit} 天前，仍在维护")
        else:
            result.maintain_score = 0
            result.reasons.append(f"最近一次提交在 {result.days_since_commit} 天前，维护基本停滞")
    if result.archived:
        result.maintain_score = 0
        result.reasons.append("仓库已归档，不再更新")

    readme = github.file_text(full_name, "README.md") or github.file_text(full_name, "README.rst")
    result.calls += 1 if readme else 0
    sample_files = _pick_source_files(paths)
    texts: list[tuple[str, str]] = [("README", readme)] if readme else []
    for path in sample_files[:3]:
        text = github.file_text(full_name, path, max_bytes=12000)
        if text:
            result.calls += 1
            texts.append((path, text))
    blob = "\n".join(text for _, text in texts).lower()

    hits_posix = sorted({mark for mark in POSIX_ONLY if mark in blob})
    hits_windows = sorted({mark for mark in WINDOWS_HINTS if mark in blob})
    if hits_posix and not hits_windows:
        result.windows = "存疑"
        result.windows_evidence = hits_posix[:4]
        result.reasons.append("源码/文档里出现 POSIX 专有写法且没有 Windows 相关说明：" +
                              "、".join(hits_posix[:4]))
    elif hits_windows:
        result.windows = "有支持迹象"
        result.windows_evidence = hits_windows[:4]
        result.reasons.append("发现 Windows 适配迹象：" + "、".join(hits_windows[:4]))
    else:
        result.windows = "未发现平台限制"

    for label, patterns in RISKY.items():
        found: list[str] = []
        for path, text in texts:
            for pattern in patterns:
                for match in re.finditer(pattern, text or "", re.I):
                    snippet = (text[max(0, match.start() - 40): match.end() + 40]
                               .replace("\n", " ").strip())
                    found.append(f"{path}: {snippet[:120]}")
        if found:
            result.risks.append({"kind": label, "samples": found[:3]})

    terms_hits = []
    blob_full = f"{(meta.get('description') or '').lower()}\n{blob}"
    for keyword, note in TERMS_RISK.items():
        if keyword in blob_full and TERMS_CONTEXT.search(blob_full):
            terms_hits.append(f"{keyword}：{note}")
    if terms_hits:
        result.risks.append({"kind": "平台条款风险", "samples": terms_hits[:3]})

    # 只在 README 装说明里出现 curl|bash，不算高危；出现在代码里才算
    def samples_of(kind: str) -> list[str]:
        for item in result.risks:
            if item["kind"] == kind:
                return item["samples"]
        return []

    exec_kinds = ("远程脚本执行", "提权或关闭防护")
    exec_samples = [sample for kind in exec_kinds for sample in samples_of(kind)]
    exec_in_code = [sample for sample in exec_samples if not sample.startswith("README")]
    if exec_in_code:
        result.risk_level = "高"
    elif exec_samples or any(len(item["samples"]) for item in result.risks):
        result.risk_level = "中"
    else:
        result.risk_level = "低"

    result.integration_kind = _kind_from_meta(meta, tree, result.dependency_files)
    result.recommended_state, result.verdict = _verdict(result)

    catalog.upsert({
        "name": full_name,
        "kind": result.integration_kind,
        "purpose": (meta.get("description") or "").strip()[:300],
        "repo_url": meta.get("html_url") or "",
        "docs_url": meta.get("homepage") or "",
        "license": result.license,
        "stars": result.stars,
        "last_commit": result.last_commit,
        "runtime": f"{meta.get('language') or '未知'}；依赖文件：{', '.join(result.dependency_files) or '未见'}",
        "inputs": "见仓库文档",
        "outputs": "见仓库文档",
        "permissions": "评估阶段未安装、未运行",
        "state": result.recommended_state,
        "risk": f"风险等级 {result.risk_level}；" + "；".join(
            f"{item['kind']}（{len(item['samples'])} 处样例）" for item in result.risks) if result.risks else f"风险等级 {result.risk_level}",
        "failure_policy": "评估失败时保留目录里的既有结论，不自动切换状态",
        "notes": result.verdict,
        "last_checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    for key, ok, value in (
        ("license", bool(result.license_ok), result.license),
        ("maintenance", result.maintain_score > 0,
         f"{result.days_since_commit} 天前最后提交，stars={result.stars}"),
        ("windows", result.windows != "存疑", result.windows),
        ("tests", result.has_tests, "有 tests/" if result.has_tests else "未见测试目录"),
        ("ci", result.has_ci, "有 workflows" if result.has_ci else "未见 CI"),
        ("risk", result.risk_level == "低", f"{result.risk_level}：{len(result.risks)} 类命中"),
    ):
        catalog.add_evidence(full_name, key, value, ok)
    catalog.add_event(full_name, "evaluate", result.recommended_state != "E", result.verdict)
    return result


def _verdict(result: Evaluation) -> tuple[str, str]:
    if result.risk_level == "高":
        kinds = "、".join(item["kind"] for item in result.risks
                          if item["kind"] in ("远程脚本执行", "提权或关闭防护"))
        return "E", (f"命中「{kinds}」：它的安装方式是从网络下载并执行脚本，"
                     "和你定的「不运行来源不明的脚本」冲突。"
                     "真要采用，就改走源码或包管理器安装，并把安装脚本逐行看过")
    if any(item["kind"] == "平台条款风险" for item in result.risks):
        return "B", ("代码本身没发现风险特征，但涉及平台使用条款限制："
                     "接入前必须先确认合规性，不要用在国内不可用的平台上")
    if result.license_ok is None:
        return "B", "许可证无法自动判定，需要人工打开 LICENSE 确认后再决定"
    if result.license_ok is False:
        return "E", f"许可证为 {result.license}，不满足直接集成条件，只能作为思路参考"
    if result.maintain_score == 0:
        return "E", "超过一年没有提交或仓库已归档，不建议依赖"
    if result.windows == "存疑":
        return "B", "许可证与维护状态没问题，但存在平台兼容疑点，需要先在本机试装再决定"
    if result.risk_level == "中":
        return "B", "可用，但有需要留意的代码特征，集成前要逐条看过样例再决定"
    return "B", "许可证宽松、维护活跃、没有发现风险特征，可以进入安装验证环节"


def _kind_from_meta(meta: dict[str, Any], tree: list[dict[str, Any]],
                    dep_files: list[str]) -> str:
    blob = " ".join([
        (meta.get("name") or "").lower(),
        (meta.get("description") or "").lower(),
        " ".join(meta.get("topics") or []),
    ])
    paths = [item.get("path", "") for item in tree]
    if "mcp" in blob and "server" in blob:
        return "MCP 服务"
    if "skill" in blob and "codex" in blob:
        return "技能/指令包"
    if "package.json" in " ".join(dep_files) and "pyproject.toml" not in dep_files:
        return "独立应用"
    if meta.get("language") == "Python":
        return "Python 库"
    return "仅参考" if not paths else "独立应用"


def _pick_source_files(paths: list[str]) -> list[str]:
    candidates = [p for p in paths
                  if p.endswith((".py", ".js", ".ts", ".ps1", ".sh")) and p.count("/") <= 2]
    priority = [p for p in candidates if any(
        key in p.lower() for key in ("install", "setup", "main", "cli", "util", "core", "run"))]
    return (priority + candidates)[:6]


def _days_since(iso: str) -> int | None:
    if not iso:
        return None
    try:
        moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, (datetime.now(timezone.utc) - moment).days)
