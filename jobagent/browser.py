"""带调试端口的浏览器：自己起一个独立配置目录，登录态长期保留。"""

from __future__ import annotations

import json
import os
import socket
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
    # Local AppData Windows (动态适配当前 Windows 登录用户)
    str(Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe"),
    # Linux / macOS 备用路径
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
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
    raise FileNotFoundError(
        "未检测到 Google Chrome 浏览器！\n"
        "AI Job Hunter 严格限定使用 Google Chrome 独立实例。\n"
        "请确认已安装 Chrome，或在 config.toml 的 [browser] 段中设置 exe = '...' 指定路径。"
    )


def is_up(port: int = DEFAULT_PORT, timeout: float = 1.0) -> bool:
    try:
        cdp.http_json(port, "/json/version", timeout=timeout)
        return True
    except OSError:
        return False


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def find_free_port(start_port: int = DEFAULT_PORT) -> int:
    """寻找本地回环地址上真正空闲的端口。"""
    for port in range(start_port, start_port + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("未能在 127.0.0.1 找到可用的调试端口")


def get_session_file(user_data_dir: Path) -> Path:
    return user_data_dir.parent / "browser_session.json"


def get_managed_session(user_data_dir: Path) -> dict[str, Any] | None:
    """读取本项目管理启动的 Chrome 运行信息。"""
    session_file = get_session_file(user_data_dir)
    if not session_file.exists():
        return None
    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
        pid = int(data.get("pid", 0))
        port = int(data.get("port", 0))
        if pid and is_pid_alive(pid) and is_up(port):
            return data
    except Exception:
        pass
    return None


def build_chrome_command(
    chrome_path: str,
    port: int,
    user_data_dir: Path,
    url: str = "about:blank",
) -> list[str]:
    """构建安全加固的 Chrome 调试模式启动命令。"""
    return [
        chrome_path,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-allow-origins=http://127.0.0.1:{port},http://localhost:{port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,OptimizationHints",
        url,
    ]


def launch(
    user_data_dir: Path,
    port: int = DEFAULT_PORT,
    url: str = "about:blank",
    browser_path: str | None = None,
    wait: float = 20.0,
) -> tuple[str, int]:
    """启动带调试端口的独立 Chrome 实例；已在跑则安全复用，返回 (status, actual_port)。
    
    安全机制：
    1. 校验 browser_session.json 确认是否是本项目管理的实例；
    2. 绝不盲目附着他人或用户日常使用的其他 9222 端口 Chrome；
    3. 目标端口被外部占用时，自动分配独立空闲端口。
    """
    user_data_dir = user_data_dir.expanduser().resolve()
    user_data_dir.mkdir(parents=True, exist_ok=True)

    managed = get_managed_session(user_data_dir)
    if managed:
        return "reused", managed["port"]

    # 检查目标端口是否被非本项目程序占用
    target_port = port
    if is_up(target_port):
        # 目标端口有响应但非本项目托管的实例：自动分配动态安全端口，杜绝混淆
        target_port = find_free_port(target_port + 1)

    exe = find_browser(browser_path)
    args = build_chrome_command(
        chrome_path=exe,
        port=target_port,
        user_data_dir=user_data_dir,
        url=url,
    )

    log_path = user_data_dir.parent / "browser-launch.log"
    with open(log_path, "ab") as log_file:
        log_file.write(("\n=== launch %s on port %s ===\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), target_port)).encode("utf-8"))
        log_file.flush()
        proc = subprocess.Popen(args, stdout=log_file, stderr=subprocess.STDOUT, close_fds=True)

    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if is_up(target_port):
            # 记录托管会话元数据
            session_data = {
                "pid": proc.pid,
                "port": target_port,
                "user_data_dir": str(user_data_dir),
                "managed_by": "ai-job-hunter",
                "started_at": time.time(),
            }
            try:
                get_session_file(user_data_dir).write_text(
                    json.dumps(session_data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:
                pass
            return "started", target_port
        time.sleep(0.4)

    raise TimeoutError(f"浏览器进程已拉起但 {target_port} 端口未响应，请检查端口环境")


def ensure_page(port: int, url: str, session: cdp.CDP | None = None) -> cdp.CDP:
    """拿到一个可用的页面连接；必要时新开一个标签页。"""
    session = session or cdp.attach(port)
    if url and not (session.url() or "").startswith(url.split("?")[0][:24]):
        session.navigate(url)
        session.wait_ready()
    return session

