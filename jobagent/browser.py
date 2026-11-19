"""带调试端口的浏览器：自己起一个独立配置目录，登录态长期保留。"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from . import cdp

DEFAULT_PORT = 9222

# 浏览器规范：仅允许使用 Google Chrome，绝不调用或触碰 Microsoft Edge
CHROME_CANDIDATES = [
    # 64-bit Windows
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    # 32-bit Windows
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    # Local AppData Windows
    str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe"),
    # Linux / macOS 备用路径
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def find_browser(explicit: str | None = None) -> str:
    if explicit:
        if not Path(explicit).exists():
            raise FileNotFoundError(f"找不到浏览器可执行文件: {explicit}")
        return explicit
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    raise FileNotFoundError("未检测到 Google Chrome。本智能体严格限定使用 Google Chrome，请确认已安装。")


def is_up(port: int = DEFAULT_PORT, timeout: float = 1.0) -> bool:
    try:
        cdp.http_json(port, "/json/version", timeout=timeout)
        return True
    except OSError:
        return False


def launch(
    user_data_dir: Path,
    port: int = DEFAULT_PORT,
    url: str = "about:blank",
    browser_path: str | None = None,
    wait: float = 20.0,
) -> str:
    """起一个带调试端口的浏览器进程；已经在跑就直接复用。"""
    if is_up(port):
        return "reused"

    exe = find_browser(browser_path)
    # 必须用绝对路径：相对路径会让 Edge 回退到默认配置，
    # 结果是"正在现有浏览器会话中打开"，调试端口永远不监听。
    user_data_dir = user_data_dir.expanduser().resolve()
    user_data_dir.mkdir(parents=True, exist_ok=True)
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,OptimizationHints",
        url,
    ]
    # 注意：不要用 DETACHED_PROCESS，实测那样浏览器起不来（端口不监听）。
    # 普通 Popen 就能在本进程退出后继续存活。
    log_path = user_data_dir.parent / "browser-launch.log"
    with open(log_path, "ab") as log_file:
        log_file.write(("\n=== launch %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode("utf-8"))
        log_file.flush()
        subprocess.Popen(args, stdout=log_file, stderr=subprocess.STDOUT, close_fds=True)

    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if is_up(port):
            return "started"
        time.sleep(0.4)
    raise TimeoutError(f"浏览器起来了但 {port} 端口没响应，检查是否被占用")


def ensure_page(port: int, url: str, session: cdp.CDP | None = None) -> cdp.CDP:
    """拿到一个可用的页面连接；必要时新开一个标签页。"""
    session = session or cdp.attach(port)
    if url and not (session.url() or "").startswith(url.split("?")[0][:24]):
        session.navigate(url)
        session.wait_ready()
    return session
