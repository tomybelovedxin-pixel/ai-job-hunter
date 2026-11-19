"""配置读取：把 config.toml 里的路径、浏览器、检索范围和模型设置解析成对象。

设计上只做两件事：读文件、给默认值。任何密钥都不写进本仓库，
模型凭据要么来自环境变量，要么从 ~/.codex/config.toml 里现读现用。
"""

from __future__ import annotations

import os
import shutil
import sys
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def get_default_data_dir() -> Path:
    """获取当前使用者的系统标准独立数据目录，完全脱离源码目录，多用户完全隔离。

    优先级：
    1. 环境变量 AI_JOB_HUNTER_DATA_DIR
    2. 项目本地优先（若项目根目录下存在 config.toml 或 data/ 目录）
    3. 操作系统标准用户目录：
       - Windows: %LOCALAPPDATA%\\AIJobHunter
       - macOS: ~/Library/Application Support/AIJobHunter
       - Linux: $XDG_DATA_HOME/ai-job-hunter 或 ~/.local/share/ai-job-hunter
    """
    env_dir = os.environ.get("AI_JOB_HUNTER_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()

    # 若项目目录内已显式配置了 config.toml，支持便携模式
    if (ROOT / "config.toml").exists():
        return ROOT

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "AIJobHunter"
        return Path.home() / "AppData" / "Local" / "AIJobHunter"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AIJobHunter"
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            return Path(xdg_data) / "ai-job-hunter"
        return Path.home() / ".local" / "share" / "ai-job-hunter"


def init_user_data(data_dir: Path | None = None) -> tuple[Path, list[str]]:
    """首次运行时初始化用户独立数据空间，自动从示例生成模板（绝不覆盖已有真实数据）。"""
    target = (data_dir or get_default_data_dir()).resolve()
    target.mkdir(parents=True, exist_ok=True)
    created: list[str] = []

    # 1. config.toml
    cfg_file = target / "config.toml"
    if not cfg_file.exists():
        example_cfg = ROOT / "config.example.toml"
        if example_cfg.exists():
            shutil.copyfile(example_cfg, cfg_file)
            created.append(str(cfg_file))

    # 2. profile.json
    prof_file = target / "profile.json"
    if not prof_file.exists():
        example_prof = ROOT / "profile.example.json"
        if example_prof.exists():
            shutil.copyfile(example_prof, prof_file)
            created.append(str(prof_file))

    # 3. master_resume.json
    resume_file = target / "master_resume.json"
    if not resume_file.exists():
        example_resume = ROOT / "master_resume.example.json"
        if example_resume.exists():
            shutil.copyfile(example_resume, resume_file)
            created.append(str(resume_file))

    # 4. 必备子目录
    (target / "chrome-profile").mkdir(parents=True, exist_ok=True)
    (target / "dump").mkdir(parents=True, exist_ok=True)
    (target / "output").mkdir(parents=True, exist_ok=True)

    return target, created


def get_default_config_path() -> Path:
    data_dir = get_default_data_dir()
    if (data_dir / "config.toml").exists():
        return data_dir / "config.toml"
    if (ROOT / "config.toml").exists():
        return ROOT / "config.toml"
    return data_dir / "config.toml"


DEFAULT_CONFIG = get_default_config_path()


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
    data_dir: Path
    raw: dict[str, Any]
    profile_path: Path
    master_resume_path: Path
    db_path: Path
    dump_dir: Path
    report_path: Path
    output_dir: Path
    browser_port: int
    browser_user_data_dir: Path
    browser_exe: str
    browser_session_file: Path
    scan_query: str
    scan_city: str
    scan_pages: int
    llm: LLMSettings
    search: SearchSettings
    sources_enabled: list[str]
    confirmations_path: Path
    auto_accept_resume: bool = False

    def ensure_dirs(self) -> None:
        for path in (self.dump_dir, self.db_path.parent, self.report_path.parent,
                     self.output_dir, self.browser_user_data_dir,
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


def load(path: str | Path | None = None) -> Config:
    if path is None:
        path = get_default_config_path()
    path = Path(path)

    # 首次运行未初始化时，自动执行独立数据空间初识化
    data_dir = path.parent
    if not path.exists():
        data_dir, _ = init_user_data(data_dir)
        path = data_dir / "config.toml"

    raw: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as handle:
            raw = tomllib.load(handle)

    browser = raw.get("browser", {}) or {}
    paths = raw.get("paths", {}) or {}
    custom_data_dir = paths.get("data_dir")
    if custom_data_dir:
        c_path = Path(custom_data_dir)
        data_dir = c_path if c_path.is_absolute() else (path.parent / c_path).resolve()
    scan = raw.get("scan", {}) or {}
    llm_raw = raw.get("llm", {}) or {}
    search_raw = raw.get("search", {}) or {}
    sources_raw = raw.get("sources", {}) or {}
    features_raw = raw.get("features", {}) or {}

    def _resolve(val: str, default: str) -> Path:
        target = Path(val or default)
        if target.is_absolute():
            return target
        if (data_dir / target).exists():
            return data_dir / target
        if (ROOT / target).exists():
            return ROOT / target
        return data_dir / target

    profile_path = _resolve(raw.get("profile", {}).get("path", ""), "profile.json")
    master_resume_path = _resolve(raw.get("resume", {}).get("master_path", ""), "master_resume.json")

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

    db_path = _resolve(paths.get("db", ""), "jobs.sqlite")
    dump_dir = _resolve(paths.get("dump", ""), "dump")
    report_path = _resolve(paths.get("report", ""), "report.md")
    output_dir = _resolve(paths.get("output", ""), "output")
    confirmations_path = _resolve(paths.get("confirmations", ""), "confirmations.json")
    browser_user_data_dir = _resolve(browser.get("user_data_dir", ""), "chrome-profile")
    browser_session_file = data_dir / "browser_session.json"
    auto_accept = bool(features_raw.get("auto_accept_resume", False))

    return Config(
        path=path,
        data_dir=data_dir,
        raw=raw,
        profile_path=profile_path,
        master_resume_path=master_resume_path,
        db_path=db_path,
        dump_dir=dump_dir,
        report_path=report_path,
        output_dir=output_dir,
        browser_port=int(browser.get("port", 9222) or 9222),
        browser_user_data_dir=browser_user_data_dir,
        browser_exe=str(browser.get("exe", "") or ""),
        browser_session_file=browser_session_file,
        scan_query=str(scan.get("default_query", "AI 应用开发")),
        scan_city=str(scan.get("default_city", "全国")),
        scan_pages=int(scan.get("pages", 1) or 1),
        llm=llm,
        search=search,
        sources_enabled=[str(x) for x in sources_raw.get("enabled", []) or []],
        confirmations_path=confirmations_path,
        auto_accept_resume=auto_accept,
    )
