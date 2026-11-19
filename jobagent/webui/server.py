"""中文求职管理面板：标准库 http.server，绑 127.0.0.1，每个按钮都接真实功能。

写操作分两类：
- 本地整理类（抓岗位、分析、生成材料、改状态）：直接执行，结果如实返回
- 对外类（发送消息、提交申请）：需要 confirm=true，否则拦下并说明原因
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..config import ROOT, load as load_config
from ..safety import NeedsConfirmation
from ..scheduler import logs as scheduler_logs
from ..scheduler import status as scheduler_status
from ..sources import SourceError
from ..store import Store

from . import api

STATIC = Path(__file__).resolve().parent / "static"


def app_available() -> tuple[bool, str]:
    index = STATIC / "index.html"
    return index.exists(), f"面板静态文件 {'就绪' if index.exists() else '缺失：' + str(index)}"


class Handler(BaseHTTPRequestHandler):
    server_version = "jobagent-ui/0.2"
    store_path: Path

    def log_message(self, fmt: str, *args: Any) -> None:
        status = str(args[1]) if len(args) > 1 else ""
        if status.startswith(("4", "5")):
            print(f"[ui] {fmt % args}")

    # ---------------- 基础 ----------------

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"ok": False, "error": message}, status)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}

    # ---------------- 路由 ----------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path == "/api/overview":
                return self._json(api.overview(self.store_path))
            if path == "/api/jobs":
                return self._json(api.jobs(self.store_path, query))
            if path.startswith("/api/job/"):
                return self._json(api.job_detail(self.store_path, int(path.rsplit("/", 1)[-1])))
            if path == "/api/applications":
                return self._json(api.applications(self.store_path))
            if path == "/api/materials":
                return self._json(api.materials(self.store_path, query))
            if path == "/api/resumes":
                return self._json(api.resumes(self.store_path))
            if path == "/api/tools":
                return self._json(api.tools(self.store_path))
            if path == "/api/events":
                return self._json(api.events(self.store_path))
            if path == "/api/tool/events":
                return self._json(api.tool_events(self.store_path, query))
            if path == "/api/scheduler":
                return self._json({"ok": True, "status": scheduler_status(),
                                   "logs": scheduler_logs(20)})
            if path == "/api/sources":
                return self._json(api.sources())
            if path == "/api/evidence":
                return self._json(api.evidence())
            if path == "/api/llm":
                return self._json(api.llm_status())
            if path == "/api/file":
                return self._file((query.get("path") or [""])[0])
            return self._error(f"没有这个接口：{path}", 404)
        except Exception as exc:
            return self._error(f"{type(exc).__name__}: {exc}", 500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        payload = self._body()
        try:
            result = api.action(self.store_path, path, payload)
            return self._json(result)
        except NeedsConfirmation as exc:
            return self._error(str(exc), 409)
        except SourceError as exc:
            return self._error(str(exc), 502)
        except KeyError:
            return self._error(f"没有这个接口：{path}", 404)
        except Exception as exc:
            return self._error(f"{type(exc).__name__}: {exc}", 500)

    # ---------------- 文件 ----------------

    def _static(self, name: str) -> None:
        target = (STATIC / name).resolve()
        try:
            target.relative_to(STATIC.resolve())
        except (ValueError, RuntimeError):
            return self._error("禁止越权路径访问", 403)
        if not target.exists() or not target.is_file():
            return self._error("文件不存在", 404)
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, raw_path: str) -> None:
        """安全下载项目生成的文件，防越权与目录穿越。"""
        if not raw_path:
            return self._error("缺少 path 参数", 400)
        target = Path(raw_path)
        if not target.is_absolute():
            target = ROOT / target
        resolved = target.resolve()
        try:
            resolved.relative_to(ROOT.resolve())
        except (ValueError, RuntimeError):
            return self._error("只允许访问项目目录内的文件", 403)

        # 绝对保护敏感凭据、数据库与个人主画像不被 WebUI 接口泄露
        forbidden_exts = {".key", ".pem", ".env", ".sqlite", ".sqlite3", ".db"}
        forbidden_files = {"profile.json", "master_resume.json", "config.toml", "evidence_claims.json"}
        if (
            resolved.suffix.lower() in forbidden_exts
            or resolved.name.lower() in forbidden_files
            or any(part.startswith(".") for part in resolved.relative_to(ROOT.resolve()).parts)
        ):
            return self._error("禁止通过 Web 接口访问系统核心配置或隐私数据文件", 403)

        if not resolved.exists() or not resolved.is_file():
            return self._error("文件不存在", 404)

        body = resolved.read_bytes()
        suffix = resolved.suffix.lower()
        content_type = {
            ".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".md": "text/markdown; charset=utf-8",
            ".json": "application/json; charset=utf-8",
        }.get(suffix, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition",
                         f"inline; filename*=UTF-8''{urllib.parse.quote(resolved.name)}")
        self.end_headers()
        self.wfile.write(body)


def serve(*, port: int = 8765, host: str = "127.0.0.1", open_browser: bool = True,
          db_path: str | Path | None = None) -> None:
    config = load_config()
    Handler.store_path = Path(db_path or config.db_path)
    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        raise SystemExit(f"端口 {port} 起不来：{exc}。换一个端口重试，例如 --port 8766")
    url = f"http://{host}:{port}/"
    print(f"管理面板已启动：{url}")
    print("按 Ctrl+C 停止。数据都落在本地 SQLite，重启后还在。")
    if open_browser:
        threading.Thread(target=lambda: (time.sleep(0.8), webbrowser.open(url)),
                         daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
