"""岗位数据源基类：统一取数、统一字段、出错不静默。

每个源自己声明：是否需要登录、有没有官方文档、允许怎么用。
取数失败时抛出 SourceError，由调用方决定是跳过还是中止。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 jobagent/0.2"
)

# 出现这些词，说明岗位可以异地做（兼职、远程、项目制、外包）
# 注意「线上」不算：线上的意思是业务在线上做，不等于岗位能远程
FLEXIBLE_HINTS = (
    "兼职", "远程", "项目制", "按单", "按项目", "外包", "在家",
    "地点不限", "不限地点", "part-time", "part time", "freelance", "contract", "remote",
)

# 本地及周边：泉州为主，含晋江/石狮等县域，以及厦门福州
LOCAL_HINTS = (
    "泉州", "晋江", "石狮", "南安", "惠安", "安溪", "德化", "永春",
    "厦门", "福州", "漳州", "莆田", "福建",
)


class SourceError(RuntimeError):
    """数据源取数失败。message 里必须能看出是网络、结构还是限流问题。"""


def city_allowed(city: str, filters: "Filters", *extra_text: str,
                 local_hints: tuple[str, ...] = LOCAL_HINTS) -> bool:
    """地点策略：本地坐班放行，外地岗位只在能远程/兼职时放行。

    规则来自你的原话——"外地远程兼职也行，泉州本地坐班"：

    - 城市命中 filters.cities（泉州/厦门/福州/远程）→ 放行；
    - 岗位文本里出现兼职/远程/项目制等灵活用工标记 → 放行；
    - 其它情况（外地坐班）→ 丢掉，不占分析队列。

    地点判不出来时放行，交给分析环节标注"需确认"，不在这里猜。
    """
    city = (city or "").strip()
    if not city:
        return True
    wanted = [c for c in (filters.cities or []) if c]
    if any(c in city for c in wanted if c not in ("远程", "remote")):
        return True
    if any(hint in city for hint in local_hints):
        return True
    blob = f"{city} " + " ".join(text or "" for text in extra_text)
    lowered = blob.lower()
    if any(hint in blob or hint in lowered for hint in FLEXIBLE_HINTS):
        return True
    if any(c and c.lower() in lowered for c in wanted):
        return True
    return False


@dataclass
class SourceResult:
    source: str
    jobs: list[dict[str, Any]] = field(default_factory=list)
    raw_count: int = 0
    note: str = ""
    fetched_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class Filters:
    """检索条件。全部来自你可以在 config.toml / profile.json 里改的配置。"""

    keywords: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    limit: int = 30
    pages: int = 1
    exclude_keywords: list[str] = field(default_factory=list)

    def match(self, text: str) -> bool:
        text = text or ""
        if any(pattern.search(text) for pattern in _patterns(self.exclude_keywords)):
            return False
        patterns = _patterns(self.keywords)
        if not patterns:
            return True
        return any(pattern.search(text) for pattern in patterns)


_ASCII_KEYWORD = re.compile(r"^[A-Za-z0-9+#./\- ]+$")


def _patterns(keywords: list[str]) -> list[re.Pattern[str]]:
    """短英文关键词按词边界匹配，避免 'AI' 命中 'email'；中文按子串匹配。"""
    out: list[re.Pattern[str]] = []
    for keyword in keywords or []:
        keyword = (keyword or "").strip()
        if not keyword:
            continue
        if _ASCII_KEYWORD.match(keyword):
            out.append(re.compile(rf"(?<![A-Za-z0-9]){re.escape(keyword)}(?![A-Za-z0-9])", re.I))
        else:
            out.append(re.compile(re.escape(keyword), re.I))
    return out


def _validate_safe_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(f"只允许 http/https 协议请求，禁止访问非法协议: {parsed.scheme}")
    if not parsed.netloc:
        raise ValueError(f"URL 缺少有效主机名: {url}")


def http_json(url: str, *, params: dict[str, Any] | None = None, timeout: float = 25,
              headers: dict[str, str] | None = None, retries: int = 2) -> Any:
    """取 JSON。失败重试，仍失败抛 SourceError（带原始错误文本）。"""
    _validate_safe_url(url)
    if params:
        query = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v not in (None, "")}, doseq=True
        )
        url = f"{url}?{query}" if "?" not in url else f"{url}&{query}"
    last_error = ""
    for attempt in range(retries + 1):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                **(headers or {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
            return json.loads(body.decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code} {exc.reason}"
            if exc.code in (400, 401, 403, 404):
                break
        except Exception as exc:  # 网络中断、超时、JSON 解析失败
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise SourceError(f"{url} 取数失败：{last_error}")


def http_post_json(url: str, *, form: dict[str, Any] | None = None,
                   json_body: dict[str, Any] | None = None,
                   extra_headers: dict[str, str] | None = None,
                   timeout: float = 25, retries: int = 2) -> Any:
    """POST 取 JSON，表单或 JSON 体二选一。"""
    _validate_safe_url(url)
    body: bytes | None = None
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        **(extra_headers or {}),
    }
    if form is not None:
        body = urllib.parse.urlencode(
            {k: v for k, v in form.items() if v is not None}
        ).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    last_error = ""
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
            return json.loads(payload.decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code} {exc.reason}"
            if exc.code in (400, 401, 403, 404):
                break
        except Exception as exc:  # 网络中断、超时、JSON 解析失败
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise SourceError(f"{url} 提交失败：{last_error}")


class Source:
    """岗位数据源。子类只需实现 fetch()。"""

    name = "base"
    label = "未命名来源"
    homepage = ""
    docs = ""
    notes = ""
    requires_login = False
    city_scoped = False  # 是否支持按城市过滤

    def fetch(self, filters: Filters) -> SourceResult:
        raise NotImplementedError

    # 子类常有的一份"原始快照"记录，方便复盘取数口径
    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "homepage": self.homepage,
            "docs": self.docs,
            "requires_login": self.requires_login,
            "city_scoped": self.city_scoped,
            "notes": self.notes,
        }


def job_dict(
    *,
    platform: str,
    external_id: str,
    title: str,
    company: str = "",
    url: str = "",
    description: str = "",
    salary_text: str = "",
    salary_min: float | None = None,
    salary_max: float | None = None,
    city: str = "",
    district: str = "",
    experience: str = "",
    education: str = "",
    tags: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """统一字段名，和 jobs 表一一对应。"""
    return {
        "platform": platform,
        "external_id": str(external_id),
        "title": (title or "").strip(),
        "company": (company or "").strip(),
        "url": url,
        "description": description,
        "salary_text": salary_text,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "city": city,
        "district": district,
        "experience": experience,
        "education": education,
        "tags": tags or [],
        "source": platform,
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "raw_card": extra or {},
    }
