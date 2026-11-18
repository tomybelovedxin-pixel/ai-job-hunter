"""手工导入：把你自己复制下来的招聘信息变成结构化岗位。

用途是补上那些没有可用公开接口的平台（BOSS、猎聘、智联等）。
你复制 JD 文本或存成网页文件，这里负责解析，不抓取、不绕过任何限制。

支持三种输入：
1. 带标签的纯文本：职位 / 公司 / 链接 / 城市 / 薪资 / 经验 / 学历，后面跟正文
2. JSON：单个对象或对象数组，键名用同一套字段
3. HTML：从网页另存的文件，标题取 <title>，正文去标签
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

from ..textutil import clean_text, html_to_text
from .base import Filters, Source, SourceError, SourceResult, job_dict

LABELS = {
    "title": ("职位", "岗位", "职位名称", "岗位名称", "title", "position", "job"),
    "company": ("公司", "公司名称", "企业", "company", "employer"),
    "url": ("链接", "网址", "url", "link", "source"),
    "city": ("城市", "地点", "工作地点", "location", "city"),
    "salary": ("薪资", "薪水", "月薪", "salary", "pay"),
    "experience": ("经验", "工作经验", "experience"),
    "education": ("学历", "education", "degree"),
    "platform": ("平台", "来源", "platform", "board"),
}

_LINE = re.compile(r"^\s*(?P<label>[^：:]{1,12})\s*[：:]\s*(?P<value>.+?)\s*$")


class ManualSource(Source):
    name = "manual"
    label = "手工导入"
    homepage = "本地"
    notes = "你自己复制/另存的招聘信息；不联网、不绕过平台限制"

    def __init__(self, paths: Iterable[str | Path] | None = None,
                 texts: Iterable[str] | None = None):
        self.paths = [Path(p) for p in (paths or [])]
        self.texts = list(texts or [])

    def fetch(self, filters: Filters) -> SourceResult:
        jobs: list[dict[str, Any]] = []
        errors: list[str] = []
        for path in self.paths:
            try:
                jobs.extend(self._from_path(path))
            except Exception as exc:
                errors.append(f"{path.name}: {type(exc).__name__} {exc}")
        for text in self.texts:
            try:
                jobs.append(self._from_text(text))
            except Exception as exc:
                errors.append(f"文本片段: {exc}")
        filtered = [item for item in jobs if filters.match(f"{item['title']} {item['description']}")]
        return SourceResult(
            source=self.name,
            jobs=filtered[: filters.limit] if filters.limit else filtered,
            raw_count=len(jobs),
            note="；".join(errors) if errors else f"导入 {len(filtered)} 条",
            error="；".join(errors) if errors and not filtered else "",
        )

    # ---------- 解析 ----------

    def _from_path(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            raise SourceError(f"文件不存在: {path}")
        suffix = path.suffix.lower()
        if suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else [data]
            return [self._from_json(item, path) for item in items]
        raw = path.read_text(encoding="utf-8", errors="replace")
        if suffix in (".html", ".htm"):
            return [self._from_html(raw, path)]
        return [self._from_text(raw, path)]

    def _from_json(self, item: dict[str, Any], path: Path) -> dict[str, Any]:
        external = str(item.get("external_id") or item.get("id")
                       or f"manual-{abs(hash(json.dumps(item, ensure_ascii=False, sort_keys=True))) % 10**10}")
        return job_dict(
            platform=str(item.get("platform") or "manual"),
            external_id=external,
            title=str(item.get("title") or item.get("职位") or ""),
            company=str(item.get("company") or item.get("公司") or ""),
            url=str(item.get("url") or item.get("链接") or ""),
            description=clean_text(str(item.get("description") or item.get("jd") or "")),
            salary_text=str(item.get("salary_text") or item.get("salary") or ""),
            city=str(item.get("city") or item.get("城市") or ""),
            experience=str(item.get("experience") or ""),
            education=str(item.get("education") or ""),
            tags=[str(t) for t in (item.get("tags") or [])],
            extra={"imported_from": str(path)},
        )

    def _from_html(self, raw: str, path: Path) -> dict[str, Any]:
        match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
        title = clean_text(match.group(1)) if match else path.stem
        text = html_to_text(raw)
        stale = time.strftime("%Y%m%d-%H%M%S")
        return job_dict(
            platform="manual",
            external_id=f"manual-{path.stem}-{stale}",
            title=title,
            description=text,
            url="",
            extra={"imported_from": str(path)},
        )

    def _from_text(self, raw: str, path: Path | None = None) -> dict[str, Any]:
        text = clean_text(raw)
        fields: dict[str, str] = {}
        body_lines: list[str] = []
        for line in text.split("\n"):
            match = _LINE.match(line)
            key = None
            if match:
                label = match.group("label").strip().lower()
                for field_name, aliases in LABELS.items():
                    if label in [alias.lower() for alias in aliases]:
                        key = field_name
                        break
            if key and key not in fields:
                fields[key] = match.group("value").strip()
            else:
                body_lines.append(line)
        body = clean_text("\n".join(body_lines))
        title = fields.get("title") or self._guess_title(text)
        if not title:
            raise SourceError("没解析出职位名称，请在文本里加一行「职位：xxx」")
        platform = (fields.get("platform") or "manual").strip().lower().replace(" ", "")
        stamp = path.stem if path else f"paste-{time.strftime('%m%d%H%M%S')}"
        return job_dict(
            platform=platform,
            external_id=f"{platform}-{stamp}-{abs(hash(body)) % 10**8}",
            title=title,
            company=fields.get("company", ""),
            url=fields.get("url", ""),
            description=body,
            salary_text=fields.get("salary", ""),
            city=fields.get("city", ""),
            experience=fields.get("experience", ""),
            education=fields.get("education", ""),
            tags=[],
            extra={"imported_from": str(path) if path else "paste"},
        )

    @staticmethod
    def _guess_title(text: str) -> str:
        for line in clean_text(text).split("\n")[:5]:
            line = line.strip()
            if 3 <= len(line) <= 40 and not _LINE.match(line):
                return line
        return ""
