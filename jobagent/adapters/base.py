"""适配器基类：定义流水线需要的四个动作。"""

from __future__ import annotations

from typing import Any

from ..cdp import CDP


class AdapterError(RuntimeError):
    """适配器执行失败（选择器变了、页面结构不对等）。"""


class BlockedError(AdapterError):
    """平台风控：要求登录、验证码、访问频繁。"""


class Adapter:
    name = "base"
    home = "about:blank"

    def __init__(self, session: CDP, *, dump_dir: Any = None, verbose: bool = True):
        self.session = session
        self.dump_dir = dump_dir
        self.verbose = verbose

    # ---------- 供子类实现 ----------

    def scan(self, query: str, *, city: str | None = None, pages: int = 1,
             limit: int | None = None, **kwargs: Any) -> list[dict[str, Any]]:
        raise NotImplementedError

    def fetch_detail(self, job: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def send_greeting(self, job: dict[str, Any], text: str, *, dry_run: bool = True) -> dict[str, Any]:
        raise NotImplementedError

    def accept_resume_request(self, company: str, hr_name: str | None = None,
                              *, dry_run: bool = True) -> dict[str, Any]:
        """识别并同意 HR 发起的简历附件投递请求。"""
        raise NotImplementedError

    def check_block(self) -> str | None:
        """返回风控/登录提示文本；正常返回 None。"""
        raise NotImplementedError

    # ---------- 公用工具 ----------

    def log(self, message: str) -> None:
        if self.verbose:
            print(f"[{self.name}] {message}", flush=True)

    def dump_html(self, tag: str) -> str | None:
        """存一份页面快照，方便选择器失配时离线校准。"""
        if not self.dump_dir:
            return None
        from pathlib import Path
        import time

        directory = Path(self.dump_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.name}-{tag}-{time.strftime('%Y%m%d-%H%M%S')}.html"
        try:
            path.write_text(self.session.html(), encoding="utf-8")
        except Exception:
            return None
        return str(path)
