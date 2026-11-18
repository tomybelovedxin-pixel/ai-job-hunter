"""工具注册中心：登记可调用的能力、权限、失败处理，并做冒烟验证。

只有 state='D' 且 auto_call=True 的工具才允许被智能体自主调用；
调用过程一律记进 tool_events，失败按各自声明的策略处理（降级或明确报错）。
"""

from __future__ import annotations

import importlib.util
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import ROOT
from .catalog import Catalog


class ToolError(RuntimeError):
    """工具调用失败。message 里说明失败原因和降级建议。"""


@dataclass
class ToolSpec:
    name: str
    purpose: str
    capability: str
    kind: str
    permissions: str
    inputs: str
    outputs: str
    runtime: str
    entry: str
    failure_policy: str
    func: Callable[..., Any]
    verify: Callable[[], tuple[bool, str]]
    repo: str = ""
    docs: str = ""
    license: str = ""
    auto_call: bool = True
    fallback: str = ""
    tags: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name, "purpose": self.purpose, "capability": self.capability,
            "kind": self.kind, "permissions": self.permissions, "inputs": self.inputs,
            "outputs": self.outputs, "runtime": self.runtime, "entry": self.entry,
            "failure_policy": self.failure_policy, "repo": self.repo, "docs": self.docs,
            "license": self.license,
            "auto_call": self.auto_call, "fallback": self.fallback, "tags": self.tags,
        }


REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    REGISTRY[spec.name] = spec
    return spec


def get(name: str) -> ToolSpec:
    if name not in REGISTRY:
        raise ToolError(f"没有登记这个工具：{name}")
    return REGISTRY[name]


def list_tools() -> list[ToolSpec]:
    return list(REGISTRY.values())


# ---------------------------------------------------------------- 调用

def call(name: str, *, catalog: Catalog | None = None, allow_unverified: bool = False,
         **kwargs: Any) -> dict[str, Any]:
    """按登记信息调用工具。未验证的工具默认不允许自动调用。"""
    spec = get(name)
    state = "D"
    if catalog is not None:
        tool = catalog.get(name)
        if tool:
            state = tool.state
            if state == "E":
                catalog.add_event(name, "blocked", False,
                                  f"工具处于暂停状态：{tool.risk or tool.notes}")
                raise ToolError(f"{name} 处于暂停状态：{tool.risk or tool.notes}")
            if not tool.auto_call and not allow_unverified:
                catalog.add_event(name, "blocked", False, "该工具不允许自动调用")
                raise ToolError(
                    f"{name} 已登记但不允许自动调用（{tool.permissions}）。"
                    f"如果要跑，请由你显式执行对应命令：{tool.entry}"
                )
            if state != "D" and not (allow_unverified and state == "C"):
                catalog.add_event(name, "blocked", False, f"状态 {state} 未通过验证")
                raise ToolError(
                    f"{name} 当前状态是 {state}（{tool.state_label}），没有通过验证，"
                    f"不能自主调用。先跑 `python -m jobagent tool verify {name}`。"
                )
    started = time.time()
    try:
        result = spec.func(**kwargs)
    except ToolError:
        raise
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        if catalog is not None:
            catalog.add_event(name, "call", False, detail)
        raise ToolError(f"{name} 调用失败（{detail}）。{spec.failure_policy}")
    seconds = time.time() - started
    if catalog is not None:
        catalog.add_event(name, "call", True, f"用时 {seconds:.2f}s")
    return {"tool": name, "ok": True, "seconds": round(seconds, 3),
            "capability": spec.capability, "result": result}


# ---------------------------------------------------------------- 同步与验证

def sync_catalog(catalog: Catalog, *, verify: bool = False) -> dict[str, Any]:
    """把注册表里的工具写进目录，必要时顺手验证。"""
    for spec in REGISTRY.values():
        catalog.upsert({
            "name": spec.name,
            "kind": spec.kind,
            "purpose": spec.purpose,
            "repo_url": spec.repo,
            "docs_url": spec.docs,
            "license": spec.license,
            "inputs": spec.inputs,
            "outputs": spec.outputs,
            "runtime": spec.runtime,
            "entry": spec.entry,
            "permissions": spec.permissions,
            "failure_policy": spec.failure_policy,
            "state": catalog.get(spec.name).state if catalog.get(spec.name) else "C",
            "source": "内置注册表",
        }, source="内置注册表")
    if verify:
        return verify_all(catalog)
    return {"tools": len(REGISTRY)}


def verify(name: str, catalog: Catalog) -> dict[str, Any]:
    spec = get(name)
    started = time.time()
    try:
        ok, detail = spec.verify()
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    seconds = time.time() - started
    catalog.add_evidence(name, "verify", detail, ok)
    if ok:
        catalog.set_state(name, "D", note=f"验证通过：{detail}",
                          installed=True, verified=True,
                          auto_call=spec.auto_call)
    else:
        catalog.set_state(name, "C",
                          note=f"验证未通过：{detail}（{spec.failure_policy}）",
                          installed=True, verified=False, auto_call=False)
    catalog.add_event(name, "verify", ok, detail)
    return {"name": name, "ok": ok, "detail": detail, "seconds": round(seconds, 2)}


def verify_all(catalog: Catalog, *, only: list[str] | None = None) -> dict[str, Any]:
    names = only or list(REGISTRY)
    results = [verify(name, catalog) for name in names]
    return {
        "passed": [item["name"] for item in results if item["ok"]],
        "failed": [{"name": item["name"], "detail": item["detail"]}
                   for item in results if not item["ok"]],
        "total": len(results),
    }


# ---------------------------------------------------------------- 内置工具实现

def _has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _tool_html_to_text(**kwargs: Any) -> Any:
    from ..textutil import html_to_text

    html = kwargs.get("html") or ""
    text = html_to_text(html)
    backend = "beautifulsoup4" if _has("bs4") else "标准库正则（降级）"
    return {"text": text, "backend": backend, "degraded": not _has("bs4")}


def _verify_html() -> tuple[bool, str]:
    from ..textutil import html_to_text

    sample = "<p>岗位职责</p><ul><li>接口对接</li></ul>"
    text = html_to_text(sample)
    ok = "岗位职责" in text and "接口对接" in text
    return ok, f"HTML 转文本正常（后端 {'bs4' if _has('bs4') else '标准库'}）：{text[:30]!r}"


def _tool_similarity(**kwargs: Any) -> Any:
    from ..sources.dedupe import similarity

    left = kwargs.get("left") or ""
    right = kwargs.get("right") or ""
    score = similarity(left, right)
    return {"score": round(score, 2), "backend": "rapidfuzz" if _has("rapidfuzz") else "difflib（降级）",
            "degraded": not _has("rapidfuzz")}


def _verify_similarity() -> tuple[bool, str]:
    from ..sources.dedupe import similarity

    same = similarity("AI 应用开发工程师", "AI 应用开发工程师")
    different = similarity("AI 应用开发工程师", "销售代表")
    ok = same >= 99 and different < same
    return ok, f"相似度可用（相同 {same:.0f}，不同 {different:.0f}），后端 " \
               f"{'rapidfuzz' if _has('rapidfuzz') else 'difflib'}"


def _tool_pdf_write(**kwargs: Any) -> Any:
    from ..resume.render import render_pdf
    from ..resume.tailor import ResumeDoc

    doc = kwargs.get("doc")
    path = kwargs.get("path")
    if not isinstance(doc, ResumeDoc) or not path:
        raise ToolError("pdf_write 需要 doc(ResumeDoc) 与 path")
    return {"path": render_pdf(doc, path)}


def _verify_pdf_write() -> tuple[bool, str]:
    from ..resume.render import render_pdf
    from ..resume.tailor import ResumeDoc

    sample = ResumeDoc(
        direction="AI 应用开发",
        basics={"name": "测试", "city": "泉州"},
        headline="测试",
        summary="这是渲染冒烟测试。",
        skill_groups=[{"label": "后端", "items": "Python · SQLite"}],
        projects=[{"name": "样例项目", "period": "2026.10", "role": "独立完成",
                   "bullets": ["写了一条用于验证中文排版与分页的样例内容。"], "grade": "A"}],
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "smoke.pdf"
        render_pdf(sample, path)
        size = path.stat().st_size
        pages = None
        try:
            from pypdf import PdfReader

            pages = len(PdfReader(str(path)).pages)
        except Exception:
            pages = None
    ok = size > 1500
    return ok, f"生成 PDF {size} 字节" + (f"，共 {pages} 页" if pages else "，未用 pypdf 复核")


def _tool_docx_write(**kwargs: Any) -> Any:
    from ..resume.render import render_docx
    from ..resume.tailor import ResumeDoc

    doc = kwargs.get("doc")
    path = kwargs.get("path")
    if not isinstance(doc, ResumeDoc) or not path:
        raise ToolError("docx_write 需要 doc(ResumeDoc) 与 path")
    return {"path": render_docx(doc, path)}


def _verify_docx_write() -> tuple[bool, str]:
    import zipfile

    from ..resume.render import render_docx
    from ..resume.tailor import ResumeDoc

    sample = ResumeDoc(
        direction="AI 应用开发",
        basics={"name": "测试", "city": "泉州"},
        headline="测试",
        summary="这是渲染冒烟测试。",
        projects=[{"name": "样例项目", "period": "2026.10", "role": "独立完成",
                   "bullets": ["验证中文写入与样式。"], "grade": "A"}],
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "smoke.docx"
        render_docx(sample, path)
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
        document = archive_read = ""
        with zipfile.ZipFile(path) as archive:
            document = archive.read("word/document.xml").decode("utf-8", "replace")
        archive_read = "word/document.xml" in "\n".join(names)
    ok = archive_read and "测试" in document
    return ok, f"DOCX 结构完整，正文含中文（{len(document)} 字符 XML）"


def _tool_pdf_text(**kwargs: Any) -> Any:
    from pypdf import PdfReader

    path = kwargs.get("path")
    if not path:
        raise ToolError("pdf_text 需要 path")
    reader = PdfReader(str(path))
    pages = [(page.extract_text() or "") for page in reader.pages]
    return {"pages": len(pages), "text": "\n".join(pages)}


def _verify_pdf_text() -> tuple[bool, str]:
    if not _has("pypdf"):
        return False, "没装 pypdf"
    from ..resume.render import render_pdf
    from ..resume.tailor import ResumeDoc

    sample = ResumeDoc(direction="AI 应用开发", basics={"name": "文本提取测试"},
                       headline="", summary="", projects=[
                           {"name": "P", "period": "", "role": "",
                            "bullets": ["这一句必须能被提取出来。"], "grade": "A"}])
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "text.pdf"
        render_pdf(sample, path)
        from pypdf import PdfReader

        text = "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
    ok = "必须能被提取" in text.replace(" ", "")
    return ok, f"PDF 文本可提取：{text.strip()[:40]!r}"


def _tool_http_json(**kwargs: Any) -> Any:
    from ..sources.base import http_json

    url = kwargs.get("url")
    if not url:
        raise ToolError("http_json 需要 url")
    return {"data": http_json(url, params=kwargs.get("params"), timeout=float(kwargs.get("timeout", 25)))}


def _verify_http_json() -> tuple[bool, str]:
    from ..sources.base import http_json

    data = http_json("https://api.github.com/rate_limit", timeout=20)
    ok = isinstance(data, dict) and "resources" in data
    return ok, "公开接口请求正常（GitHub rate_limit）"


def _tool_github_search(**kwargs: Any) -> Any:
    from . import github

    query = kwargs.get("query")
    if not query:
        raise ToolError("github_search 需要 query")
    repos = github.search_repos(query, per_page=int(kwargs.get("per_page", 5)))
    return {"count": len(repos),
            "items": [{"full_name": r.get("full_name"), "stars": r.get("stargazers_count"),
                       "license": ((r.get("license") or {}) or {}).get("spdx_id"),
                       "pushed_at": r.get("pushed_at"),
                       "description": (r.get("description") or "")[:160]} for r in repos]}


def _verify_github_search() -> tuple[bool, str]:
    from . import github

    data = github.rate_limit()
    core = (data or {}).get("resources", {}).get("core", {})
    remaining = core.get("remaining")
    return core.get("limit") is not None, f"GitHub API 可达，剩余配额 {remaining}"


def _tool_text_quality(**kwargs: Any) -> Any:
    from ..textquality import scan

    return scan(kwargs.get("text") or "")


def _verify_text_quality() -> tuple[bool, str]:
    from ..textquality import scan

    result = scan("综上所述，这个方案赋能了整个闭环。")
    kinds = result.get("issue_kinds", [])
    return bool(kinds), f"命中痕迹：{'、'.join(kinds)}"


def _tool_natural_writing_skill(**kwargs: Any) -> Any:
    root = Path(r"C:\Users\ASUS\Documents\ChatGPT\去ai味道")
    skill = root / "src" / "skills" / "natural-writing" / "SKILL.md"
    rules = Path(r"C:\Users\ASUS\.codex\AGENTS.md")
    return {
        "skill_path": str(skill), "skill_exists": skill.exists(),
        "global_rules": str(rules), "global_rules_exists": rules.exists(),
        "usage": "在改写简历、求职信或招聘私信时按其规则执行；只在本机 Codex 生效",
    }


def _verify_natural_writing() -> tuple[bool, str]:
    skill = Path(r"C:\Users\ASUS\Documents\ChatGPT\去ai味道\src\skills\natural-writing\SKILL.md")
    rules = Path(r"C:\Users\ASUS\.codex\AGENTS.md")
    ok = skill.exists() and rules.exists()
    return ok, f"技能文件 {'在' if skill.exists() else '不在'}，全局规则 {'在' if rules.exists() else '不在'}"


def _tool_job_collect(**kwargs: Any) -> Any:
    from ..collect import collect
    from ..sources import Filters, get as get_source
    from ..store import Store

    store = Store(kwargs.get("db_path") or (ROOT / "data" / "jobs.sqlite"))
    filters = Filters(
        keywords=list(kwargs.get("keywords") or []),
        cities=list(kwargs.get("cities") or []),
        limit=int(kwargs.get("limit", 25)),
        pages=int(kwargs.get("pages", 1)),
        exclude_keywords=list(kwargs.get("exclude_keywords") or []),
    )
    names = kwargs.get("sources") or ["tencent", "remoteok", "jobicy", "remotive", "hn"]
    report = collect(store, [get_source(name) for name in names], filters)
    result = report.as_dict()
    store.close()
    return result


def _verify_job_collect() -> tuple[bool, str]:
    from ..sources import Filters, get as get_source
    from ..store import Store

    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "smoke.sqlite")
        source = get_source("tencent")
        result = source.fetch(Filters(keywords=["AI"], cities=["深圳"], limit=3))
        store.close()
    return bool(result.jobs), f"腾讯招聘接口取到 {len(result.jobs)} 条岗位"


def _tool_jd_analyze(**kwargs: Any) -> Any:
    from ..analyze import analyze_text

    text = kwargs.get("text") or ""
    if not text.strip():
        raise ToolError("jd_analyze 需要 text（岗位 JD 正文）")
    return analyze_text(text, title=kwargs.get("title") or "",
                        company=kwargs.get("company") or "",
                        city=kwargs.get("city") or "",
                        salary_text=kwargs.get("salary_text") or "")


def _verify_jd_analyze() -> tuple[bool, str]:
    from ..analyze import analyze_text

    sample = (
        "岗位职责：负责大模型应用的开发与落地，使用 Python 完成接口对接与数据处理，"
        "配合前端联调，参与 RAG 检索链路搭建，要求 2 年经验，本科及以上学历。"
    )
    result = analyze_text(sample, title="AI 应用开发工程师")
    ok = bool(result.get("requirements")) and result.get("verdict")
    return ok, f"拆出 {len(result.get('requirements', []))} 条要求，结论 {result.get('verdict')}"


def _tool_resume_build(**kwargs: Any) -> Any:
    from ..resume_service import build_resume_for_job

    return build_resume_for_job(
        job_id=kwargs.get("job_id"),
        direction=kwargs.get("direction"),
        out_dir=kwargs.get("out_dir"),
        db_path=kwargs.get("db_path"),
    )


def _verify_resume_build() -> tuple[bool, str]:
    from ..resume import build_doc, render_markdown
    from ..resume.tailor import load_master

    master = load_master()
    doc = build_doc(master, direction="AI 应用开发")
    with tempfile.TemporaryDirectory() as directory:
        path = render_markdown(doc, Path(directory) / "smoke.md")
        size = Path(path).stat().st_size
    return size > 400, f"按方向裁剪并渲染 Markdown 成功（{size} 字节）"


def _tool_webui(**kwargs: Any) -> Any:
    raise ToolError("webui 需要独立进程启动，不通过工具调用接口执行；"
                    "请运行 python -m jobagent ui")


def _verify_webui() -> tuple[bool, str]:
    from ..webui.server import app_available

    ok, detail = app_available()
    return ok, detail


def _tool_scheduler(**kwargs: Any) -> Any:
    from ..scheduler import control

    return control(kwargs.get("action") or "status")


def _verify_scheduler() -> tuple[bool, str]:
    from ..scheduler import status

    data = status()
    return True, f"调度器可读状态：{data.get('running') and '运行中' or '未运行'}"


def _tool_report(**kwargs: Any) -> Any:
    from ..report import write_report

    return write_report(kwargs.get("db_path"))


def _verify_report() -> tuple[bool, str]:
    from ..report import build_report

    text = build_report()
    return len(text) > 50, f"报告可生成（{len(text)} 字）"


def _register_builtins() -> None:
    register(ToolSpec(
        name="html_to_text",
        license="MIT（beautifulsoup4）",
        purpose="把招聘页面的 HTML 正文转成纯文本，供 JD 解析使用",
        capability="JD 解析",
        kind="Python 库",
        permissions="无（纯本地计算）",
        inputs="html: str",
        outputs="text: str；backend: 实际使用的解析器",
        runtime="可选依赖 beautifulsoup4；缺失时自动降级为标准库正则",
        entry="jobagent.textutil.html_to_text",
        failure_policy="bs4 解析异常时退回标准库实现，结果里标 degraded=True",
        repo="https://github.com/wention/BeautifulSoup4",
        docs="https://www.crummy.com/software/BeautifulSoup/bs4/doc/",
        func=_tool_html_to_text,
        verify=_verify_html,
        fallback="标准库正则实现",
        tags=["岗位", "解析"],
    ))
    register(ToolSpec(
        name="fuzzy_similarity",
        license="MIT（rapidfuzz）",
        purpose="岗位标题与公司名的模糊比对，用于跨平台查重",
        capability="岗位查重",
        kind="Python 库",
        permissions="无（纯本地计算）",
        inputs="left: str, right: str",
        outputs="score: 0-100",
        runtime="可选依赖 rapidfuzz；缺失时用标准库 difflib",
        entry="jobagent.sources.dedupe.similarity",
        failure_policy="rapidfuzz 不可用时改用 difflib，口径不变",
        repo="https://github.com/rapidfuzz/RapidFuzz",
        docs="https://rapidfuzz.github.io/RapidFuzz/",
        func=_tool_similarity,
        verify=_verify_similarity,
        fallback="difflib.SequenceMatcher",
        tags=["岗位", "查重"],
    ))
    register(ToolSpec(
        name="http_json",
        purpose="按统一规则请求公开 JSON 接口（限速、重试、错误留痕）",
        capability="数据采集",
        kind="内置能力",
        permissions="只读网络（仅公开接口）",
        inputs="url: str, params: dict",
        outputs="解析后的 JSON",
        runtime="Python 标准库 urllib",
        entry="jobagent.sources.base.http_json",
        failure_policy="重试 2 次后抛 SourceError，错误文本包含 HTTP 状态码",
        func=_tool_http_json,
        verify=_verify_http_json,
        tags=["采集"],
    ))
    register(ToolSpec(
        name="github_search",
        purpose="在 GitHub 检索候选开源项目，支撑工具发现环节",
        capability="开源工具发现",
        kind="外部 API",
        permissions="只读网络（GitHub 公开 API；有 GITHUB_TOKEN 时提高限额）",
        inputs="query: str, per_page: int",
        outputs="仓库列表（名称、star、许可证、最近提交）",
        runtime="Python 标准库；可选 GITHUB_TOKEN 环境变量",
        entry="jobagent.tools.github.search_repos",
        failure_policy="限流或网络失败时返回明确错误，提示设置 GITHUB_TOKEN 或稍后重试",
        repo="https://github.com/PyGithub/PyGithub",
        docs="https://docs.github.com/rest",
        func=_tool_github_search,
        verify=_verify_github_search,
        tags=["工具发现"],
    ))
    register(ToolSpec(
        name="pdf_write",
        license="BSD-3-Clause（reportlab）",
        purpose="把简历渲染成 PDF（中文字体、分页、无截断）",
        capability="简历产出",
        kind="Python 库",
        permissions="写本地文件",
        inputs="doc: ResumeDoc, path: str",
        outputs="生成的 PDF 路径",
        runtime="依赖 reportlab；中文字体用系统自带 msyh.ttc",
        entry="jobagent.resume.render.render_pdf",
        failure_policy="缺依赖或缺字体时抛 RenderError，改为只出 Markdown 并在结果里说明",
        repo="https://github.com/MrBitBucket/reportlab-mirror",
        docs="https://docs.reportlab.com/",
        func=_tool_pdf_write,
        verify=_verify_pdf_write,
        tags=["简历"],
    ))
    register(ToolSpec(
        name="docx_write",
        license="MIT（python-docx）",
        purpose="把简历渲染成可编辑的 DOCX",
        capability="简历产出",
        kind="Python 库",
        permissions="写本地文件",
        inputs="doc: ResumeDoc, path: str",
        outputs="生成的 DOCX 路径",
        runtime="依赖 python-docx",
        entry="jobagent.resume.render.render_docx",
        failure_policy="缺依赖时抛 RenderError，改为只出 Markdown",
        repo="https://github.com/python-openxml/python-docx",
        docs="https://python-docx.readthedocs.io/",
        func=_tool_docx_write,
        verify=_verify_docx_write,
        tags=["简历"],
    ))
    register(ToolSpec(
        name="pdf_text",
        license="BSD-3-Clause（pypdf）",
        purpose="读取 PDF 文本，用于检查简历有没有被截断",
        capability="文档校验",
        kind="Python 库",
        permissions="读本地文件",
        inputs="path: str",
        outputs="pages: int, text: str",
        runtime="依赖 pypdf",
        entry="jobagent.resume.check",
        failure_policy="缺依赖时跳过 PDF 文本校验，并在检查结果里注明",
        repo="https://github.com/py-pdf/pypdf",
        docs="https://pypdf.readthedocs.io/",
        func=_tool_pdf_text,
        verify=_verify_pdf_text,
        tags=["简历", "校验"],
    ))
    register(ToolSpec(
        name="text_quality_scan",
        purpose="扫描中文文本里的机器腔与空话，给改写提供依据",
        capability="文本质量",
        kind="内置能力",
        permissions="无（纯本地计算）",
        inputs="text: str",
        outputs="issue 列表与可读性统计",
        runtime="Python 标准库正则",
        entry="jobagent.textquality.scan",
        failure_policy="纯本地规则，不会失败；命中项一律只提示不改写",
        func=_tool_text_quality,
        verify=_verify_text_quality,
        tags=["文本", "简历"],
    ))
    register(ToolSpec(
        name="natural_writing_skill",
        purpose="调用本机已安装的自然表达技能与全局规则（改简历、求职信、招聘私信）",
        capability="文本质量",
        kind="技能/指令包",
        permissions="读本机 Codex 配置目录（只读）",
        inputs="无",
        outputs="技能文件与全局规则的位置及可用状态",
        runtime="本机 Codex 插件 local-natural-expression",
        entry="C:/Users/ASUS/.codex/plugins/cache/local-natural-expression",
        failure_policy="文件缺失时提示重新安装插件恢复，不影响其它功能",
        repo="https://github.com/ASUS/去ai味道（本机项目）",
        func=_tool_natural_writing_skill,
        verify=_verify_natural_writing,
        auto_call=False,
        tags=["文本"],
    ))
    register(ToolSpec(
        name="job_collect",
        purpose="从公开数据源批量取岗位并落库（含去重与可疑标记）",
        capability="岗位采集",
        kind="内置能力",
        permissions="只读网络 + 写本地数据库",
        inputs="keywords: list, cities: list, limit: int, sources: list",
        outputs="每个源的取数与落库统计",
        runtime="Python 标准库",
        entry="jobagent.collect.collect",
        failure_policy="单个源失败只记录错误，不影响其它源；不会重试到被封",
        func=_tool_job_collect,
        verify=_verify_job_collect,
        tags=["岗位"],
    ))
    register(ToolSpec(
        name="jd_analyze",
        purpose="拆解岗位要求并逐条对照能力档案，给出可解释的匹配结论",
        capability="岗位匹配",
        kind="内置能力",
        permissions="无（可选调用大模型，默认规则引擎）",
        inputs="text: str, title/company/city/salary_text",
        outputs="要求清单、评级、优势、风险、缺口、待确认问题、结论",
        runtime="Python 标准库；模型调用可选",
        entry="jobagent.analyze.analyze_text",
        failure_policy="模型不可用时退回规则引擎，结论里标 engine=rule",
        func=_tool_jd_analyze,
        verify=_verify_jd_analyze,
        tags=["岗位"],
    ))
    register(ToolSpec(
        name="resume_build",
        purpose="按方向或按岗位生成定制简历（DOCX/PDF/Markdown）并自检",
        capability="简历产出",
        kind="内置能力",
        permissions="写本地文件 + 写数据库",
        inputs="job_id 或 direction, out_dir",
        outputs="文件路径与检查结果",
        runtime="Python 标准库 + 可选渲染依赖",
        entry="jobagent.resume_service.build_resume_for_job",
        failure_policy="渲染依赖缺失时只出 Markdown，并明确列出缺什么",
        func=_tool_resume_build,
        verify=_verify_resume_build,
        tags=["简历"],
    ))
    register(ToolSpec(
        name="job_report",
        purpose="导出求职进度 Markdown 报告",
        capability="进度记录",
        kind="内置能力",
        permissions="写本地文件",
        inputs="db_path",
        outputs="报告路径与摘要",
        runtime="Python 标准库",
        entry="jobagent.report.write_report",
        failure_policy="数据库为空时输出空报告而不是报错",
        func=_tool_report,
        verify=_verify_report,
        tags=["记录"],
    ))
    register(ToolSpec(
        name="scheduler_control",
        purpose="启动/停止/查看定时检索任务状态",
        capability="定时任务",
        kind="内置能力",
        permissions="启动本机后台进程 + 写日志文件",
        inputs="action: start|stop|status|logs",
        outputs="调度状态与最近日志",
        runtime="Python 标准库线程 + PID 文件",
        entry="jobagent.scheduler.control",
        failure_policy="启动失败时返回真实错误；不会假装任务在跑",
        func=_tool_scheduler,
        verify=_verify_scheduler,
        auto_call=False,
        tags=["调度"],
    ))
    register(ToolSpec(
        name="webui",
        purpose="中文求职管理面板（岗位、材料、应聘记录、工具目录）",
        capability="界面",
        kind="独立应用",
        permissions="本机 HTTP 服务，只绑定 127.0.0.1",
        inputs="--port",
        outputs="浏览器页面；所有写操作走本地数据库",
        runtime="Python 标准库 http.server",
        entry="python -m jobagent ui",
        failure_policy="端口占用时明确报错并提示换端口",
        func=_tool_webui,
        verify=_verify_webui,
        auto_call=False,
        tags=["界面"],
    ))


_register_builtins()

