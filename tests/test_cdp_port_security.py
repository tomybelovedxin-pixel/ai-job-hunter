import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from jobagent.browser import launch
from jobagent.cdp import CDP


class TestCDPPortSecurity(unittest.TestCase):
    @patch("jobagent.browser.get_managed_session", return_value=None)
    @patch("jobagent.browser.verify_managed_chrome", return_value=(True, "核验通过"))
    @patch("jobagent.browser.is_up", return_value=False)
    @patch("jobagent.browser.find_browser", return_value="C:\\dummy\\chrome.exe")
    @patch("subprocess.Popen")
    @patch("time.sleep")
    def test_browser_launch_cdp_safe_flags(self, mock_sleep, mock_popen, mock_find, mock_is_up, mock_ver, _mock_session):
        import tempfile, shutil
        tmp = tempfile.mkdtemp(prefix="test_cdp_safe_")
        try:
            mock_proc = MagicMock()
            mock_proc.pid = 9999
            mock_popen.return_value = mock_proc

            # 验证启动参数严格限定本地回环地址与指定 Origin，彻底禁用通配符 *
            with patch("jobagent.browser.is_up", side_effect=[False, True]):
                launch(user_data_dir=Path(tmp) / "chrome-profile", port=9222, wait=1.0)

            args = mock_popen.call_args[0][0]
            self.assertIn("--remote-debugging-address=127.0.0.1", args)
            self.assertIn("--remote-allow-origins=http://127.0.0.1:9222,http://localhost:9222", args)
            self.assertNotIn("--remote-allow-origins=*", args)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @patch("jobagent.cdp.WebSocket._handshake")
    @patch("socket.create_connection")
    def test_cdp_websocket_sends_local_trusted_origin(self, mock_socket_conn, mock_handshake):
        mock_sock = MagicMock()
        mock_socket_conn.return_value = mock_sock

        cdp = CDP("ws://127.0.0.1:9222/devtools/page/test")
        # 确认握手时传递了本机可信 Origin
        mock_handshake.assert_called_with("127.0.0.1", 9222, "/devtools/page/test", "http://127.0.0.1:9222")


if __name__ == "__main__":
    unittest.main()
