"""纯标准库实现的 WebSocket + Chrome DevTools Protocol 客户端。

不依赖 websocket-client / pychrome，只用 socket + struct + base64。
设计目标：连上已经手工登录好的浏览器，全程只做导航与取数。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import threading
import time
import urllib.request
from typing import Any, Callable
from urllib.parse import urlparse

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BIN = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class CDPError(RuntimeError):
    """浏览器返回的协议级错误。"""


class WSClosed(RuntimeError):
    """连接被对端关闭。"""


class WebSocket:
    """够用的 RFC6455 客户端：文本帧、掩码、ping/pong、分片重组。"""

    def __init__(self, url: str, timeout: float = 30.0, origin: str | None = None):
        parsed = urlparse(url)
        if parsed.scheme != "ws":
            raise ValueError(f"只支持 ws:// 地址，收到 {url!r}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        # 默认发送本机可信 Origin，适配仅允许本机访问的 CDP 安全策略
        origin = origin or f"http://{host}:{port}"
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._buf = b""
        self._lock = threading.Lock()
        self._handshake(host, port, path, origin)

    # ---------- 握手 ----------

    def _handshake(self, host: str, port: int, path: str, origin: str | None) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if origin:
            lines.append(f"Origin: {origin}")
        request = "\r\n".join(lines) + "\r\n\r\n"
        self._sock.sendall(request.encode())

        while b"\r\n\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WSClosed("握手阶段连接被关闭")
            self._buf += chunk
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        header_text = head.decode("latin-1")
        status_line = header_text.split("\r\n", 1)[0]
        if "101" not in status_line:
            raise WSClosed(f"WebSocket 升级失败: {status_line}")

        headers = {}
        for line in header_text.split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()
        ).decode()
        got = headers.get("sec-websocket-accept", "")
        if got != expected:
            raise WSClosed(f"Sec-WebSocket-Accept 校验失败: {got!r}")

    # ---------- 收发 ----------

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(max(4096, n - len(self._buf)))
            if not chunk:
                raise WSClosed("连接被关闭")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray()
        header.append(0x80 | opcode)  # FIN=1
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._lock:
            self._sock.sendall(bytes(header) + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def _recv_message(self) -> str | None:
        """返回一条完整文本消息；控制帧内部消化；关闭返回 None。"""
        fragments: list[bytes] = []
        frag_opcode: int | None = None
        while True:
            b1, b2 = self._read_exact(2)
            fin = bool(b1 & 0x80)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length) if length else b""
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                try:
                    self._send_frame(OP_CLOSE, b"")
                except OSError:
                    pass
                return None
            if opcode in (OP_TEXT, OP_BIN):
                frag_opcode = opcode
                fragments = [payload]
            elif opcode == OP_CONT:
                fragments.append(payload)
            else:
                continue
            if fin:
                data = b"".join(fragments)
                if frag_opcode == OP_BIN:
                    return data.decode("utf-8", "replace")
                return data.decode("utf-8")
            frag_opcode = frag_opcode if frag_opcode is not None else OP_CONT

    def recv_text(self) -> str | None:
        return self._recv_message()

    def close(self) -> None:
        try:
            self._send_frame(OP_CLOSE, b"")
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


class CDP:
    """一条 WebSocket 对应一个页面目标（不用 sessionId，最简单也最稳）。"""

    def __init__(self, ws_url: str, timeout: float = 30.0, origin: str | None = None):
        parsed = urlparse(ws_url)
        origin = origin or f"http://{parsed.hostname or '127.0.0.1'}:{parsed.port or 9222}"
        self.ws = WebSocket(ws_url, timeout=timeout, origin=origin)
        self._next_id = 0
        self._events: list[dict[str, Any]] = []
        self._listeners: dict[str, list[Callable[[dict[str, Any]], None]]] = {}
        self._default_timeout = timeout

    # ---------- 事件 ----------

    def on(self, method: str, fn: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.setdefault(method, []).append(fn)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if not method:
            return
        for fn in self._listeners.get(method, ()):
            try:
                fn(msg)
            except Exception:  # 监听器自己出错不能拖垮主流程
                pass
        self._events.append(msg)
        if len(self._events) > 500:
            del self._events[:250]

    def drain_events(self) -> list[dict[str, Any]]:
        out = self._events[:]
        self._events.clear()
        return out

    # ---------- 调用 ----------

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self._next_id += 1
        msg_id = self._next_id
        payload = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params
        self.ws.send_text(json.dumps(payload))

        deadline = time.monotonic() + (timeout or self._default_timeout)
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError(f"等待 {method} 响应超时")
            raw = self.ws.recv_text()
            if raw is None:
                raise WSClosed(f"{method} 等待响应时连接被关闭")
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == msg_id:
                if "error" in msg:
                    err = msg["error"]
                    raise CDPError(f"{method} 失败: {err.get('message')} {err.get('data', '')}")
                return msg.get("result", {})
            self._dispatch(msg)

    # ---------- 常用封装 ----------

    def evaluate(
        self,
        expression: str,
        *,
        await_promise: bool = True,
        return_by_value: bool = True,
        timeout: float | None = None,
    ) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": return_by_value,
                "userGesture": True,
            },
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            desc = (detail.get("exception") or {}).get("description") or detail.get("text")
            raise CDPError(f"页面脚本抛错: {desc}")
        return (result.get("result") or {}).get("value")

    def navigate(self, url: str, timeout: float | None = None) -> None:
        self.call("Page.navigate", {"url": url}, timeout=timeout)

    def url(self) -> str:
        return self.evaluate("location.href", await_promise=False)

    def wait_ready(self, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                state = self.evaluate("document.readyState", await_promise=False, timeout=5)
                if state in ("interactive", "complete"):
                    return
            except (CDPError, TimeoutError, WSClosed):
                pass
            time.sleep(0.3)
        raise TimeoutError("等待页面 readyState 超时")

    def wait_for(self, expression: str, timeout: float = 20.0, interval: float = 0.4) -> bool:
        """轮询一个返回布尔的表达式，比依赖 loadEventFired 事件可靠。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.evaluate(expression, await_promise=False, timeout=10):
                    return True
            except (CDPError, TimeoutError, WSClosed):
                pass
            time.sleep(interval)
        return False

    def html(self) -> str:
        return self.evaluate("document.documentElement.outerHTML", await_promise=False)

    def close(self) -> None:
        self.ws.close()


def http_json(port: int, path: str, timeout: float = 5.0) -> Any:
    """读 CDP 的 HTTP 端点，例如 /json/version、/json/list。"""
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_targets(port: int) -> list[dict[str, Any]]:
    try:
        data = http_json(port, "/json/list")
    except OSError as exc:
        hint = (
            f"\n\n====================================================================\n"
            f"❌ 未检测到正在运行的 Chrome 调试端口 (http://127.0.0.1:{port})\n"
            f"👉 解决办法：\n"
            f"   - Windows 用户：请直接双击运行项目根目录的 【start_browser.bat】\n"
            f"   - macOS / Linux 用户：请在终端执行 【bash start_browser.sh】\n"
            f"   - 或手动命令启动：chrome.exe --remote-debugging-port={port} --user-data-dir=\"data/chrome-profile\"\n"
            f"浏览器启动并在页面登录你的求职账号后，重新运行本程序即可！\n"
            f"====================================================================\n"
        )
        raise CDPError(hint) from exc
    return [t for t in data if t.get("type") == "page"]


def attach(port: int, url_contains: str | None = None) -> CDP:
    """挑一个页面标签页连上去；url_contains 为空时取首个可用标签页，指定时严格核验目标。"""
    targets = list_targets(port)
    if not targets:
        raise CDPError("没有可用的页面标签页")
    if url_contains:
        for t in targets:
            if url_contains in (t.get("url") or ""):
                return CDP(t["webSocketDebuggerUrl"])
        raise CDPError(
            f"未找到 URL 包含 '{url_contains}' 的浏览器标签页。\n"
            f"当前已有标签页: {[t.get('url', '') for t in targets]}\n"
            f"为保障安全，已拒绝随机附着到其他非目标网页。"
        )
    return CDP(targets[0]["webSocketDebuggerUrl"])


if __name__ == "__main__":  # 快速自检：python -m jobagent.cdp
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9222
    for target in list_targets(port):
        print(target.get("title"), "|", target.get("url"))
    session = attach(port)
    print("title =", session.evaluate("document.title", await_promise=False))
    session.close()
