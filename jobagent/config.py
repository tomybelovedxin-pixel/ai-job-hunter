"""配置读取：把 config.toml 里的路径、浏览器、检索范围和模型设置解析成对象。

设计上只做两件事：读文件、给默认值。任何密钥都不写进本仓库，
模型凭据要么来自环境变量，要么从 ~/.codex/config.toml 里现读现用。
"""

from __future__ import annotations

import os
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.toml"


def _abs(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else (ROOT / path)


@dataclass
class LLMSettings:
    """大模型调用设置。已开启物理级防偷跑防护，默认走本地规则，不调用外部计费接口。"""

    enabled: bool = False
    allow_paid_api: bool = False
    provider: str = "local_rule"
    codex_config: Path = field(default_factory=lambda: Path.home() / ".codex" / "config.toml")
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "rule-fallback"
    timeout: float = 60.0
    max_output_tokens: int = 1500
    temperature: float = 0.3

    def resolve(self) -> dict[str, str]:
        """防偷跑安全解析：未开启外部计费许可时，绝不读取环境变量或Codex凭据。"""
        if not self.enabled or not getattr(self, "allow_paid_api", False):
            # 严格熔断：阻断外部计费调用，返回本地兜底
            return {"base_url": "http://127.0.0.1:11434/v1/", "model": "rule-fallback", "api_key": ""}

        base_url, model, api_key = self.base_url, self.model, ""
        # 仅在显式允许外部计费时才尝试解析
        env_key = os.environ.get("JOBAGENT_LLM_KEY", "")
        if env_key:
            api_key = env_key
        if not base_url or not model or not api_key:
            cfg = read_codex_config(self.codex_config)
            section = cfg.get("model_providers", {}).get(self.provider, {}) or {}
            if not base_url:
                base_url = str(section.get("base_url", "") or "")
            if not api_key:
                api_key = str(section.get("api_key") or "")
            if not model:
                model = str(cfg.get("model", "") or "")
        if base_url and not base_url.endswith("/"):
            base_url += "/"
        return {"base_url": base_url, "model": model, "api_key": api_key}


@dataclass
class SearchSettings:
    """检索范围。都是可改的默认值，来源是 profile.json 和你自己的填写。"""

    keywords: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    salary_floor: float = 0.0
    pages: int = 1
    per_source_limit: int = 30
    remote_keywords: list[str] = field(default_factory=lambda: ["远程", "remote"])
    exclude_keywords: list[str] = field(default_factory=list)


@dataclass
class Config:
    path: Path
    raw: dict[str, Any]
    profile_path: Path
    db_path: Path
    dump_dir: Path
    report_path: Path
    browser_port: int
    browser_user_data_dir: Path
    browser_exe: str
    scan_query: str
    scan_city: str
    scan_pages: int
    llm: LLMSettings
    search: SearchSettings
    sources_enabled: list[str]
    confirmations_path: Path

    def ensure_dirs(self) -> None:
        for path in (self.dump_dir, self.db_path.parent, self.report_path.parent,
                     self.confirmations_path.parent):
            path.mkdir(parents=True, exist_ok=True)


def read_codex_config(path: str | Path) -> dict[str, Any]:
    """读 ~/.codex/config.toml。读不到就返回空 dict，由调用方决定降级方式。"""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except Exception:
        return {}


def load(path: str | Path = DEFAULT_CONFIG) -> Config:
    path = Path(path)
    raw: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as handle:
            raw = tomllib.load(handle)

    browser = raw.get("browser", {}) or {}
    paths = raw.get("paths", {}) or {}
    scan = raw.get("scan", {}) or {}
    llm_raw = raw.get("llm", {}) or {}
    search_raw = raw.get("search", {}) or {}
    sources_raw = raw.get("sources", {}) or {}

    profile_path = _abs(raw.get("profile", {}).get("path", "profile.json"))
    profile_raw: dict[str, Any] = {}
    if profile_path.exists():
        import json

        try:
            profile_raw = json.loads(profile_path.read_text(encoding="utf-8"))
        except Exception:
            profile_raw = {}

    search = SearchSettings(
        keywords=[str(x) for x in search_raw.get("keywords", []) or []],
        cities=[str(x) for x in search_raw.get("cities", []) or []],
        salary_floor=float(search_raw.get("salary_floor", 0) or 0),
        pages=int(search_raw.get("pages", scan.get("pages", 1)) or 1),
        per_source_limit=int(search_raw.get("per_source_limit", 30) or 30),
        exclude_keywords=[str(x) for x in search_raw.get("exclude_keywords", []) or []],
    )
    # 没在 config.toml 里写检索范围时，从档案里取一份初值（都是可改的）
    if not search.keywords:
        seen: list[str] = []
        for direction in profile_raw.get("directions", []):
            for keyword in direction.get("keywords", [])[:2]:
                keyword = str(keyword).strip()
                if keyword and keyword not in seen:
                    seen.append(keyword)
        search.keywords = seen[:8]
    if not search.cities:
        search.cities = [str(x) for x in profile_raw.get("city_preferred", [])][:4]
    if not search.salary_floor:
        search.salary_floor = float(profile_raw.get("salary_floor", 0) or 0)

    llm = LLMSettings(
        enabled=bool(llm_raw.get("enabled", False)),
        allow_paid_api=bool(llm_raw.get("allow_paid_api", False)),
        provider=str(llm_raw.get("provider", "local_rule")),
        codex_config=Path(str(llm_raw.get("codex_config")
                             or Path.home() / ".codex" / "config.toml")),
        base_url=str(llm_raw.get("base_url", "") or ""),
        model=str(llm_raw.get("model", "") or ""),
        timeout=float(llm_raw.get("timeout", 60) or 60),
        max_output_tokens=int(llm_raw.get("max_output_tokens", 1500) or 1500),
        temperature=float(llm_raw.get("temperature", 0.3) or 0.3),
    )

    return Config(
        path=path,
        raw=raw,
        profile_path=profile_path,
        db_path=_abs(paths.get("db", "data/jobs.sqlite")),
        dump_dir=_abs(paths.get("dump", "data/dump")),
        report_path=_abs(paths.get("report", "data/report.md")),
        browser_port=int(browser.get("port", 9222) or 9222),
        browser_user_data_dir=_abs(browser.get("user_data_dir", "data/browser-profile")),
        browser_exe=str(browser.get("exe", "") or ""),
        scan_query=str(scan.get("default_query", "AI 应用开发")),
        scan_city=str(scan.get("default_city", "泉州")),
        scan_pages=int(scan.get("pages", 1) or 1),
        llm=llm,
        search=search,
        sources_enabled=[str(x) for x in sources_raw.get("enabled", []) or []],
        confirmations_path=_abs(paths.get("confirmations", "data/confirmations.json")),
    )
