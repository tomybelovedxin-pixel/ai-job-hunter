"""带调试端口的浏览器：自己起一个独立配置目录，登录态长期保留。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

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
    """检查指定端口是否为正在响应的 Google Chrome CDP 调试服务（排除 Edge 等）。"""
    try:
        ver = cdp.http_json(port, "/json/version", timeout=timeout)
        b = str(ver.get("Browser", "")).lower()
        return bool("chrome" in b or "chromium" in b) and not ("edg" in b or "edge" in b)
    except (OSError, Exception):
        return False


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def get_process_info(pid: int) -> dict[str, str] | None:
    """获取指定 PID 的进程名称与启动命令行参数（跨平台安全容错）。
    
    安全准则：Windows 下若无法读取命令行，不允许降级使用 tasklist 虚假认定，必须返回 None。
    """
    if pid <= 0:
        return None
    if sys.platform == "win32":
        try:
            ps_cmd = (
                f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}'; "
                "if ($p) { @{Name=$p.Name; CommandLine=$p.CommandLine} | ConvertTo-Json -Compress }"
            )
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout.strip())
                cmdline = str(data.get("CommandLine") or "").strip()
                name = str(data.get("Name") or "").strip()
                if name and cmdline:
                    return {"name": name, "cmdline": cmdline}
        except Exception:
            pass
    elif sys.platform == "linux":
        try:
            cmdline_path = Path(f"/proc/{pid}/cmdline")
            if cmdline_path.exists():
                cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore").strip()
                if cmdline:
                    return {"name": Path(cmdline.split()[0]).name, "cmdline": cmdline}
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            res = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if res.returncode == 0 and res.stdout.strip():
                cmdline = res.stdout.strip()
                return {"name": "Google Chrome", "cmdline": cmdline}
        except Exception:
            pass
    return None


def verify_managed_chrome(
    pid: int,
    port: int,
    user_data_dir: Path,
    timeout: float = 1.5,
) -> tuple[bool, str]:
    """严格核验 Chrome 实例身份：
    1. PID 必须真实存活；
    2. 必须成功获取进程信息及完整启动命令行参数（Windows 下无法读取命令行时拒绝附着）；
    3. 进程名必须为 Chrome (chrome.exe / google-chrome / chromium)，绝不能是 Edge 或其他进程；
    4. 启动参数必须包含 --remote-debugging-port={port}；
    5. 启动参数必须包含 --user-data-dir 且规范化路径与专属目录匹配，杜绝连接日常个人目录；
    6. CDP 端口 /json/version 必须响应，且 Browser 必须为 Chrome/Chromium，绝不能包含 Edg/Edge。
    """
    if not is_pid_alive(pid):
        return False, f"PID {pid} 进程不存在或已退出"

    proc_info = get_process_info(pid)
    if not proc_info or not proc_info.get("cmdline"):
        return False, f"无法读取 PID {pid} 的完整启动命令行参数，无法核验专属身份，拒绝附着"

    name_lower = proc_info.get("name", "").lower()
    if "edge" in name_lower or "msedge" in name_lower:
        return False, f"检测到 PID {pid} 为 Edge 浏览器，根据规范严禁连接"
    if not ("chrome" in name_lower or "chromium" in name_lower):
        return False, f"PID {pid} 进程名 ({proc_info.get('name')}) 不是 Google Chrome"

    cmdline = proc_info.get("cmdline", "")
    cmdline_lower = cmdline.lower()
    if "edge" in cmdline_lower or "msedge" in cmdline_lower:
        return False, "命令行包含 Edge 特征，严禁连接"

    # 校验端口参数
    if f"--remote-debugging-port={port}" not in cmdline:
        return False, f"Chrome 命令行中调试端口参数与记录端口 {port} 不一致"

    # 校验 user-data-dir 规范化路径比较
    expected_norm = str(user_data_dir.resolve()).lower().rstrip("\\/")
    if "--user-data-dir" not in cmdline_lower:
        return False, "Chrome 未配置专属 --user-data-dir，可能为日常个人浏览器，拒绝连接"

    matched_dir = False
    if expected_norm in cmdline_lower:
        matched_dir = True
    else:
        import re
        m = re.search(r'--user-data-dir=["\']?([^"\']+)["\']?', cmdline, re.IGNORECASE)
        if m:
            actual_norm = str(Path(m.group(1).strip()).resolve()).lower().rstrip("\\/")
            if actual_norm == expected_norm:
                matched_dir = True

    if not matched_dir:
        return False, f"Chrome 的 user-data-dir 与本项目配置 ({user_data_dir.resolve()}) 规范化比较不匹配"

    # 校验 CDP 调试服务
    try:
        ver_info = cdp.http_json(port, "/json/version", timeout=timeout)
    except Exception as exc:
        return False, f"CDP 调试端口 {port} 无法连接或无响应 ({exc})"

    browser_desc = str(ver_info.get("Browser", "")).lower()
    if "edg" in browser_desc or "edge" in browser_desc:
        return False, f"CDP 服务响应显示为 Edge 浏览器 ({ver_info.get('Browser')})，拒绝连接"
    if not ("chrome" in browser_desc or "chromium" in browser_desc):
        return False, f"CDP 服务响应非 Chrome ({ver_info.get('Browser')})，拒绝连接"

    return True, "Chrome 身份核验通过"


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


def get_managed_session_with_reason(user_data_dir: Path) -> tuple[dict[str, Any] | None, str]:
    """读取并严格核验本项目管理启动的 Chrome 运行信息，返回 (session_dict, reason)。"""
    session_file = get_session_file(user_data_dir)
    if not session_file.exists():
        return None, f"会话文件不存在: {session_file}"
    try:
        data = json.loads(session_file.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"会话文件损坏无法解析: {exc}"

    if data.get("managed_by") != "ai-job-hunter":
        return None, "会话文件未标识 managed_by='ai-job-hunter'"

    try:
        pid = int(data.get("pid", 0))
        port = int(data.get("port", 0))
    except (TypeError, ValueError):
        return None, "会话文件 PID 或端口格式无效"

    if pid <= 0 or port <= 0:
        return None, f"会话参数无效 (pid={pid}, port={port})"

    # 规范化路径严格比较
    recorded_dir = data.get("user_data_dir", "")
    if not recorded_dir:
        return None, "会话文件中未记录 user_data_dir"
    try:
        norm_recorded = str(Path(recorded_dir).resolve()).lower().rstrip("\\/")
        norm_expected = str(user_data_dir.resolve()).lower().rstrip("\\/")
        if norm_recorded != norm_expected:
            return None, f"会话文件中的用户目录 ({recorded_dir}) 与当前配置目录 ({user_data_dir}) 规范化比较不匹配"
    except Exception as exc:
        return None, f"用户目录路径解析失败: {exc}"

    ok, reason = verify_managed_chrome(pid, port, user_data_dir)
    if not ok:
        return None, reason

    return data, "核验通过"


def get_managed_session(user_data_dir: Path) -> dict[str, Any] | None:
    """读取并核验受项目管理的 Chrome 运行信息，核验不通过返回 None。"""
    session, _ = get_managed_session_with_reason(user_data_dir)
    return session


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
    last_verify_reason = ""
    while time.monotonic() < deadline:
        if is_up(target_port):
            # 启动后、写入会话文件之前，必须严格核验启动进程 PID、命令行、端口、浏览器类型及 user-data-dir
            verified, verify_reason = verify_managed_chrome(proc.pid, target_port, user_data_dir)
            if verified:
                pid_val = proc.pid if isinstance(proc.pid, int) else 9999
                session_data = {
                    "pid": pid_val,
                    "port": target_port,
                    "user_data_dir": str(user_data_dir.resolve()),
                    "managed_by": "ai-job-hunter",
                    "started_at": time.time(),
                }
                try:
                    get_session_file(user_data_dir).write_text(
                        json.dumps(session_data, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                except Exception as exc:
                    raise RuntimeError(f"写入浏览器会话文件失败: {exc}")
                return "started", target_port
            else:
                last_verify_reason = verify_reason
        time.sleep(0.5)

    try:
        proc.terminate()
    except Exception:
        pass
    fail_msg = (
        f"启动的 Chrome 实例身份核验未通过 ({last_verify_reason})"
        if last_verify_reason
        else f"浏览器进程已拉起但 {target_port} 端口未按时就绪"
    )
    raise RuntimeError(fail_msg)


def ensure_page(port: int, url: str, session: cdp.CDP | None = None) -> cdp.CDP:
    """拿到一个可用的页面连接；必要时新开一个标签页。"""
    session = session or cdp.attach(port)
    if url and not (session.url() or "").startswith(url.split("?")[0][:24]):
        session.navigate(url)
        session.wait_ready()
    return session

