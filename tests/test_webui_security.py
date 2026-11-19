import unittest
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from jobagent.webui.server import Handler
from jobagent.config import ROOT


class TestWebUISecurity(unittest.TestCase):
    def setUp(self):
        self.handler = Handler.__new__(Handler)
        self.handler._error = MagicMock()
        self.handler.send_response = MagicMock()
        self.handler.send_header = MagicMock()
        self.handler.end_headers = MagicMock()
        self.handler.wfile = MagicMock()
        self.handler.headers = {}
        self.handler.client_address = ("127.0.0.1", 12345)

    def test_webui_file_forbidden_access(self):
        # 1. 尝试目录穿越/符号链接逃逸访问根目录之外的实际文件
        outside_file = str(Path(sys.executable).resolve())
        self.handler._file(outside_file)
        self.handler._error.assert_called_with("只允许访问项目目录内的文件（禁止符号链接逃逸）", 403)

        # 2. 尝试读取系统隐私配置 profile.json
        self.handler._error.reset_mock()
        self.handler._file("profile.example.json")
        # profile.example.json 是公开模板文件，应正常返回或被允许，而 profile.json 必须被禁止
        self.handler._file("profile.json")
        self.handler._error.assert_called_with("禁止通过 Web 接口访问系统核心配置或隐私数据文件", 403)

        # 3. 尝试读取密钥与凭证文件
        self.handler._error.reset_mock()
        self.handler._file(".env")
        self.handler._error.assert_called_with("禁止通过 Web 接口访问系统核心配置或隐私数据文件", 403)

        # 4. 尝试读取数据库文件
        self.handler._error.reset_mock()
        self.handler._file("data/jobs.sqlite")
        self.handler._error.assert_called_with("禁止通过 Web 接口访问系统核心配置或隐私数据文件", 403)

        # 5. 不存在的文件
        self.handler._error.reset_mock()
        self.handler._file("non_existent_file.pdf")
        self.handler._error.assert_called_with("文件不存在", 404)

    def test_non_local_client_ip_blocked(self):
        # 外部局域网 IP (如 192.168.1.100) 尝试访问 WebUI
        self.handler.client_address = ("192.168.1.100", 5555)
        self.handler.path = "/api/overview"
        self.handler.do_GET()
        self.handler._error.assert_called_with("未授权访问：WebUI 仅限本机访问 (127.0.0.1)", 403)

    def test_csrf_origin_referer_protection(self):
        # 来自恶意跨站 Origin 的 POST 请求
        self.handler.client_address = ("127.0.0.1", 12345)
        self.handler.headers = {"Origin": "https://malicious-site.com"}
        self.handler.path = "/api/application"
        self.handler.do_POST()
        self.handler._error.assert_called_with("CSRF 来源校验失败，拒绝执行该写操作", 403)

        # 伪造跨站 Referer
        self.handler._error.reset_mock()
        self.handler.headers = {"Referer": "https://evil-hacker.com/attack.html"}
        self.handler.do_POST()
        self.handler._error.assert_called_with("CSRF 来源校验失败，拒绝执行该写操作", 403)

    def test_request_body_size_limit(self):
        # 请求体大小超出 2MB 限制
        self.handler.headers = {
            "Origin": "http://127.0.0.1:8765",
            "Content-Length": str(5 * 1024 * 1024)  # 5MB
        }
        self.handler.path = "/api/application"
        self.handler.do_POST()
        self.handler._error.assert_called_with("请求数据过大（限制 2MB）", 413)


if __name__ == "__main__":
    unittest.main()
