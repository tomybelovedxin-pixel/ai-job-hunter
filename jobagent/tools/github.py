"""GitHub 只读客户端：搜索仓库、读元数据、看文件树、取许可证。

只用公开 API，不下载、不执行任何仓库代码。
有 GITHUB_TOKEN 环境变量就用（提高限额），没有就走匿名限流（每小时 60 次）。
响应会缓存到 data/github_cache/，避免同一轮任务重复请求。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..config import ROOT

API = "https://api.github.com"
CACHE_DIR = ROOT / "data" / "github_cache"
DEFAULT_TTL = 3600 * 6


class GitHubError(RuntimeError):
    """GitHub 取数失败：限流、404 或网络问题，错误文本里带原始信息。"""


def _headers() -> dict[str, str]:
    headers = {
        "User-Agent": "jobagent-tool-eval/0.2",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _cache_path(path: str, params: dict[str, Any] | None) -> Path:
    key = path.strip("/").replace("/", "_")
    if params:
        key += "_" + urllib.parse.urlencode(sorted(params.items())).replace("=", "-")
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in key)
    return CACHE_DIR / f"{safe[:120]}.json"


def get(path: str, params: dict[str, Any] | None = None, *, ttl: int = DEFAULT_TTL,
        use_cache: bool = True) -> Any:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = _cache_path(path, params)
    if use_cache and cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < ttl:
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except Exception:
                pass
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    request = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            remaining = response.headers.get("X-RateLimit-Remaining")
            body = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        if exc.code == 403 and "rate limit" in detail.lower():
            raise GitHubError(
                "GitHub 匿名限流用完了（每小时 60 次）。"
                "设置环境变量 GITHUB_TOKEN 之后重跑，或者等一会儿再试。"
            )
        raise GitHubError(f"GitHub 返回 HTTP {exc.code}：{detail}")
    except Exception as exc:
        raise GitHubError(f"GitHub 请求失败：{type(exc).__name__}: {exc}")
    if use_cache:
        cache_file.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    if remaining is not None:
        try:
            if int(remaining) == 0:
                raise GitHubError("GitHub 配额已用尽，本次结果可能不完整")
        except ValueError:
            pass
    return body


def search_repos(query: str, *, per_page: int = 10, sort: str = "stars",
                 order: str = "desc") -> list[dict[str, Any]]:
    data = get("/search/repositories", {
        "q": query, "per_page": per_page, "sort": sort, "order": order,
    }, ttl=3600 * 12)
    return list((data or {}).get("items") or [])


def repo(full_name: str) -> dict[str, Any]:
    return get(f"/repos/{full_name}", ttl=3600 * 6) or {}


def license_of(full_name: str) -> dict[str, Any]:
    try:
        return get(f"/repos/{full_name}/license", ttl=3600 * 24) or {}
    except GitHubError:
        return {}


def tree(full_name: str, ref: str | None = None) -> list[dict[str, Any]]:
    try:
        data = get(f"/repos/{full_name}/git/trees/{ref or 'HEAD'}", {"recursive": "1"},
                   ttl=3600 * 12)
    except GitHubError:
        try:
            info = repo(full_name)
            data = get(f"/repos/{full_name}/git/trees/{info.get('default_branch', 'main')}",
                       {"recursive": "1"}, ttl=3600 * 12)
        except GitHubError:
            return []
    return list((data or {}).get("tree") or [])


def file_text(full_name: str, path: str, *, max_bytes: int = 40000) -> str:
    try:
        data = get(f"/repos/{full_name}/contents/{path}", ttl=3600 * 12)
    except GitHubError:
        return ""
    if isinstance(data, dict) and data.get("encoding") == "base64":
        import base64

        raw = base64.b64decode(data.get("content", "")).decode("utf-8", "replace")
        return raw[:max_bytes]
    return ""


def rate_limit() -> dict[str, Any]:
    try:
        return get("/rate_limit", ttl=60) or {}
    except GitHubError as exc:
        return {"error": str(exc)}
