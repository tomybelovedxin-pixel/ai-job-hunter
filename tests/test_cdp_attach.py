import unittest
from unittest.mock import patch
from jobagent.cdp import attach, CDPError


class TestCDPAttach(unittest.TestCase):
    def test_cdp_attach_strict_url_check(self):
        mock_targets = [
            {"type": "page", "url": "https://www.bing.com", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/page/1"},
            {"type": "page", "url": "https://mail.google.com", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/page/2"},
        ]

        with patch("jobagent.cdp.list_targets", return_value=mock_targets):
            # 1. 查找存在的标签页
            with patch("jobagent.cdp.CDP") as mock_cdp:
                mock_cdp.return_value = "cdp_instance"
                res = attach(9222, url_contains="bing.com")
                self.assertEqual(res, "cdp_instance")
                mock_cdp.assert_called_with("ws://127.0.0.1:9222/page/1")

            # 2. 查找不存在的目标（如 zhipin.com），严禁盲目回退附着到 bing 或 mail！
            with self.assertRaises(CDPError) as exc_info:
                attach(9222, url_contains="zhipin.com")

            self.assertIn("未找到 URL 包含 'zhipin.com' 的浏览器标签页", str(exc_info.exception))
            self.assertIn("拒绝随机附着", str(exc_info.exception))


if __name__ == "__main__":
    unittest.main()
