import unittest
from unittest.mock import MagicMock
from jobagent.webui.server import Handler


class TestWebUISecurity(unittest.TestCase):
    def test_webui_file_forbidden_access(self):
        handler = Handler.__new__(Handler)
        handler._error = MagicMock()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.wfile = MagicMock()

        # 1. 尝试目录穿越访问根目录之外
        handler._file("../../Windows/win.ini")
        handler._error.assert_called_with("只允许访问项目目录内的文件", 403)

        # 2. 尝试读取系统隐私配置 profile.json
        handler._error.reset_mock()
        handler._file("profile.json")
        handler._error.assert_called_with("禁止通过 Web 接口访问系统核心配置或隐私数据文件", 403)

        # 3. 尝试读取密钥与凭证文件
        handler._error.reset_mock()
        handler._file(".env")
        handler._error.assert_called_with("禁止通过 Web 接口访问系统核心配置或隐私数据文件", 403)

        # 4. 尝试读取数据库文件
        handler._error.reset_mock()
        handler._file("data/jobs.sqlite")
        handler._error.assert_called_with("禁止通过 Web 接口访问系统核心配置或隐私数据文件", 403)


if __name__ == "__main__":
    unittest.main()
