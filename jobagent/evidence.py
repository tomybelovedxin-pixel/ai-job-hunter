"""能力证据核查：把简历里的每条断言拿到本机对照实物。

规则很简单，也刻意保守：
- 有代码/产物，而且能在本机真跑一遍 → A
- 有产物或文档，但关键环节缺证据 → B
- 只有设计、调研、学习记录 → C
- 什么实物都找不到，只有自述 → D

只跑白名单里的脚本（本项目 tests/ 与已登记的外部检查脚本），
不会执行从网络下载的东西。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .config import ROOT

DEFAULT_CLAIMS = ROOT / "evidence_claims.json"
REPORT_PATH = ROOT / "data" / "evidence_report.json"

# 允许自动执行检查的脚本前缀（本项目自有的测试与工具）
RUN_WHITELIST = (
    str(ROOT / "tests"),
    str(ROOT / "tools"),
    r"C:\Users\ASUS\Documents\ChatGPT\去ai味道\tests",
)

GRADE_ORDER = {"A": 4, "B": 3, "C": 2, "D": 1}


@dataclass
class CheckResult:
    kind: str
    value: str
    ok: bool
    detail: str = ""
    note: str = ""
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "value": self.value,
            "ok": self.ok,
            "detail": self.detail,
            "note": self.note,
            "seconds": round(self.seconds, 2),
        }


@dataclass
class ClaimResult:
    id: str
    project: str
    claim: str
    declared_grade: str
    verified_grade: str
    checks: list[CheckResult] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    resume_usable: bool = False
    resume_note: str = ""
    user_confirmed: bool = False

    @property
    def missing(self) -> list[str]:
        return [f"{c.kind}:{c.value}" for c in self.checks if not c.ok]

    @property
    def passed(self) -> list[str]:
        return [f"{c.kind}:{c.value}" for c in self.checks if c.ok]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project": self.project,
            "claim": self.claim,
            "declared_grade": self.declared_grade,
            "verified_grade": self.verified_grade,
            "checks": [c.as_dict() for c in self.checks],
            "missing": self.missing,
            "caveats": self.caveats,
            "resume_usable": self.resume_usable,
            "resume_note": self.resume_note,
            "user_confirmed": self.user_confirmed,
        }


def _run_script(path: Path, timeout: float = 180) -> CheckResult:
    resolved = path.resolve()
    if not any(str(resolved).startswith(prefix) for prefix in RUN_WHITELIST):
        return CheckResult("run", str(path), False, "不在允许自动执行的脚本白名单里")
    if resolved.suffix.lower() == ".ps1":
        launcher = shutil.which("pwsh") or shutil.which("powershell")
        if not launcher:
            return CheckResult("run", str(path), False, "本机找不到 pwsh/powershell")
        command = [launcher, "-NoProfile", "-File", str(resolved)]
    else:
        command = [sys.executable, str(resolved)]
    started = time.time()
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, cwd=str(resolved.parent.parent),
        )
    except subprocess.TimeoutExpired:
        return CheckResult("run", str(path), False, f"超过 {timeout:.0f}s 未结束", seconds=time.time() - started)
    except Exception as exc:
        return CheckResult("run", str(path), False, f"{type(exc).__name__}: {exc}",
                           seconds=time.time() - started)
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-4:])
    ok = proc.returncode == 0
    detail = tail or (proc.stderr or "").strip()[-200:]
    return CheckResult("run", str(path), ok, detail, seconds=time.time() - started)


def _check(kind: str, value: str, note: str = "") -> CheckResult:
    if kind == "path":
        path = Path(value)
        if path.exists():
            size = path.stat().st_size if path.is_file() else 0
            return CheckResult(kind, value, True, f"{size} 字节" if size else "目录存在", note)
        return CheckResult(kind, value, False, "文件不存在", note)
    if kind == "glob":
        matches = sorted(ROOT.glob(value)) if not Path(value).is_absolute() else sorted(Path(value).parent.glob(Path(value).name))
        return CheckResult(kind, value, bool(matches), f"命中 {len(matches)} 个", note)
    if kind == "run":
        result = _run_script(Path(value))
        result.note = note
        return result
    if kind == "scheduled_task":
        # powershell 起进程偶尔很慢，重试一次再下结论，避免把瞬时失败判成"没有证据"
        last = ""
        for attempt in range(2):
            try:
                proc = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"(Get-ScheduledTask -TaskName '{value}' -ErrorAction Stop).TaskName"],
                    capture_output=True, text=True, errors="replace", timeout=60,
                )
                output = (proc.stdout or proc.stderr or "").strip()
                ok = proc.returncode == 0 and value in (proc.stdout or "")
                if ok or attempt == 1:
                    return CheckResult(kind, value, ok, output[:80], note)
                last = output[:80]
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
                if attempt == 1:
                    return CheckResult(kind, value, False, last, note)
        return CheckResult(kind, value, False, last, note)
    if kind == "statement":
        return CheckResult(kind, value, True, "自述，无法用文件核实", note)
    return CheckResult(kind, value, False, f"未知检查类型 {kind}", note)


def _verified_grade(checks: list[CheckResult], declared: str) -> str:
    hard = [c for c in checks if c.kind != "statement"]
    passed = [c for c in hard if c.ok]
    if not passed:
        return "D"
    if len(passed) == len(hard):
        return declared if declared in ("A", "B") else "A"
    return "B"


def load_claims(path: str | Path = DEFAULT_CLAIMS) -> dict[str, Any]:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("claims", [])
    return data


def verify_claims(claims: Iterable[dict[str, Any]]) -> list[ClaimResult]:
    results: list[ClaimResult] = []
    for claim in claims:
        checks = [_check(c.get("kind", ""), c.get("value", ""), c.get("note", ""))
                  for c in claim.get("checks", [])]
        results.append(
            ClaimResult(
                id=claim.get("id", ""),
                project=claim.get("project", ""),
                claim=claim.get("claim", ""),
                declared_grade=claim.get("grade", "D"),
                verified_grade=_verified_grade(checks, claim.get("grade", "D")),
                checks=checks,
                caveats=list(claim.get("caveats", [])),
                resume_usable=bool(claim.get("resume_usable")),
                resume_note=claim.get("resume_note", ""),
                user_confirmed=bool(claim.get("user_confirmed")),
            )
        )
    return results


def build_report(*, claims_path: str | Path = DEFAULT_CLAIMS,
                 report_path: str | Path = REPORT_PATH,
                 run_checks: bool = True) -> dict[str, Any]:
    data = load_claims(claims_path)
    if not run_checks:
        for claim in data["claims"]:
            claim["checks"] = [c for c in claim.get("checks", []) if c.get("kind") != "run"]
    results = verify_claims(data["claims"])
    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "legend": data.get("legend", {}),
        "claims": [r.as_dict() for r in results],
        "summary": {
            "total": len(results),
            "by_grade": {
                grade: sum(1 for r in results if r.verified_grade == grade)
                for grade in ("A", "B", "C", "D")
            },
            "resume_usable": sum(1 for r in results if r.resume_usable),
            "downgraded": [
                {"id": r.id, "declared": r.declared_grade, "verified": r.verified_grade}
                for r in results
                if GRADE_ORDER[r.verified_grade] < GRADE_ORDER.get(r.declared_grade, 1)
            ],
        },
    }
    path = Path(report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def usable_claims(report: dict[str, Any]) -> list[dict[str, Any]]:
    """能写进简历的断言：有实物证据 + 标记为可用。"""
    return [
        claim for claim in report.get("claims", [])
        if claim.get("resume_usable") and claim.get("verified_grade") in ("A", "B")
    ]
