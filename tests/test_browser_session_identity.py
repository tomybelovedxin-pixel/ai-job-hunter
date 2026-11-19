"""Tests for Chrome browser session verification, process identity check, and connection defense."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jobagent.adapters.base import AdapterError
from jobagent.adapters.boss import BossAdapter
from jobagent.browser import (
    get_managed_session,
    get_managed_session_with_reason,
    get_session_file,
    verify_managed_chrome,
)
from jobagent.listener import ChatListener


class TestBrowserSessionIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="jobagent_test_browser_")
        self.user_data_dir = Path(self.tmp_dir) / "chrome-profile"
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.session_file = get_session_file(self.user_data_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_current_account_info_detection(self):
        """测试已登录和未登录状态下的用户信息提取。"""
        mock_session = MagicMock()
        adapter = BossAdapter(mock_session, verbose=False)

        # 模拟已登录
        mock_session.evaluate.return_value = json.dumps({
            "logged_in": True,
            "user_name": "求职测试者",
        })
        info = adapter.current_account_info()
        self.assertTrue(info["logged_in"])
        self.assertEqual(info["user_name"], "求职测试者")

        # 模拟未登录
        mock_session.evaluate.return_value = json.dumps({
            "logged_in": False,
            "user_name": "",
        })
        info_unlogin = adapter.current_account_info()
        self.assertFalse(info_unlogin["logged_in"])

    def test_conversation_identity_and_switching(self):
        """测试目标会话与当前会话不匹配时的安全切换防误操作机制。"""
        mock_session = MagicMock()
        adapter = BossAdapter(mock_session, verbose=False)
        adapter.check_block = MagicMock(return_value=None)
        adapter.dump_html = MagicMock(return_value="mock.html")

        # 当前打开错误公司A，目标为正确公司B
        adapter.conversation_history = MagicMock(side_effect=[
            {"pane": True, "company": "错误公司A", "name": "HR李", "job": "前端"},
            {"pane": True, "company": "正确公司B", "name": "HR王", "job": "Python"},
        ])
        adapter.open_conversation = MagicMock(return_value="已选中：正确公司B")
        adapter.detect_resume_request = MagicMock(return_value={"ok": True, "cards": []})

        res = adapter.accept_resume_request("正确公司B", dry_run=True)
        adapter.open_conversation.assert_called_with("正确公司B")
        self.assertEqual(res["status"], "no_request")

    @patch("jobagent.browser.is_pid_alive", return_value=True)
    @patch("jobagent.browser.get_process_info")
    @patch("jobagent.cdp.http_json")
    def test_verify_managed_chrome_valid(self, mock_http, mock_proc, _mock_alive):
        """测试受托管合规 Chrome 实例核验通过。"""
        mock_proc.return_value = {
            "name": "chrome.exe",
            "cmdline": (
                f"chrome.exe --remote-debugging-port=9222 "
                f"--user-data-dir={self.user_data_dir}"
            ),
        }
        mock_http.return_value = {"Browser": "Chrome/120.0.6099.130"}

        ok, reason = verify_managed_chrome(1234, 9222, self.user_data_dir)
        self.assertTrue(ok)
        self.assertIn("通过", reason)

    @patch("jobagent.browser.is_pid_alive", return_value=True)
    @patch("jobagent.browser.get_process_info")
    def test_verify_managed_chrome_rejects_edge(self, mock_proc, _mock_alive):
        """严格拒绝连接 Microsoft Edge 浏览器。"""
        # 进程名为 Edge
        mock_proc.return_value = {
            "name": "msedge.exe",
            "cmdline": "msedge.exe --remote-debugging-port=9222",
        }
        ok, reason = verify_managed_chrome(1234, 9222, self.user_data_dir)
        self.assertFalse(ok)
        self.assertIn("Edge", reason)

    @patch("jobagent.browser.is_pid_alive", return_value=True)
    @patch("jobagent.browser.get_process_info")
    @patch("jobagent.cdp.http_json")
    def test_verify_managed_chrome_rejects_cdp_edge(self, mock_http, mock_proc, _mock_alive):
        """若 CDP 服务响应显示为 Edge 特征，同样严格拒绝。"""
        mock_proc.return_value = {
            "name": "chrome.exe",
            "cmdline": f"chrome.exe --remote-debugging-port=9222 --user-data-dir={self.user_data_dir}",
        }
        mock_http.return_value = {"Browser": "Edg/120.0.2210.91"}

        ok, reason = verify_managed_chrome(1234, 9222, self.user_data_dir)
        self.assertFalse(ok)
        self.assertIn("Edge", reason)

    @patch("jobagent.browser.is_pid_alive", return_value=True)
    @patch("jobagent.browser.get_process_info")
    def test_verify_managed_chrome_user_data_dir_mismatch(self, mock_proc, _mock_alive):
        """若 Chrome 运行在日常个人目录而非本项目专属目录，坚决拒绝连接。"""
        mock_proc.return_value = {
            "name": "chrome.exe",
            "cmdline": "chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\Users\\User\\AppData\\Local\\Google\\Chrome\\User Data",
        }
        ok, reason = verify_managed_chrome(1234, 9222, self.user_data_dir)
        self.assertFalse(ok)
        self.assertIn("不匹配", reason)

    @patch("jobagent.browser.is_pid_alive", return_value=True)
    @patch("jobagent.browser.get_process_info")
    def test_verify_managed_chrome_port_mismatch(self, mock_proc, _mock_alive):
        """若命令行参数中端口与请求端口不一致，拒绝连接。"""
        mock_proc.return_value = {
            "name": "chrome.exe",
            "cmdline": f"chrome.exe --remote-debugging-port=9999 --user-data-dir={self.user_data_dir}",
        }
        ok, reason = verify_managed_chrome(1234, 9222, self.user_data_dir)
        self.assertFalse(ok)
        self.assertIn("不一致", reason)

    @patch("jobagent.browser.is_pid_alive", return_value=True)
    @patch("jobagent.browser.get_process_info", return_value=None)
    def test_verify_managed_chrome_cmdline_read_failure(self, _mock_proc, _mock_alive):
        """Windows 下无法读取命令行时（例如普通 tasklist 无法获取完整参数），必须拒绝附着。"""
        ok, reason = verify_managed_chrome(1234, 9222, self.user_data_dir)
        self.assertFalse(ok)
        self.assertIn("无法读取", reason)

    @patch("jobagent.browser.is_pid_alive", return_value=True)
    @patch("jobagent.browser.get_process_info", return_value={"name": "chrome.exe", "cmdline": ""})
    def test_verify_managed_chrome_empty_cmdline_rejected(self, _mock_proc, _mock_alive):
        """启动命令行参数为空时，无法核验专属参数，必须坚决拒绝附着。"""
        ok, reason = verify_managed_chrome(1234, 9222, self.user_data_dir)
        self.assertFalse(ok)
        self.assertIn("无法读取", reason)

    def test_get_managed_session_directory_mismatch_rejected(self):
        """会话文件中记录的 user_data_dir 与当前配置目录不一致时，严格拒绝复用。"""
        other_dir = Path(self.tmp_dir) / "other_profile"
        other_dir.mkdir(parents=True, exist_ok=True)
        session_data = {
            "pid": 9999,
            "port": 9222,
            "user_data_dir": str(other_dir),
            "managed_by": "ai-job-hunter",
        }
        self.session_file.write_text(json.dumps(session_data), encoding="utf-8")

        res, reason = get_managed_session_with_reason(self.user_data_dir)
        self.assertIsNone(res)
        self.assertIn("不匹配", reason)

    @patch("subprocess.Popen")
    @patch("jobagent.browser.find_browser", return_value="mock_chrome.exe")
    @patch("jobagent.browser.is_up", return_value=True)
    @patch("jobagent.browser.verify_managed_chrome", return_value=(False, "模拟身份校验未通过"))
    def test_launch_failure_does_not_write_session_file(self, _mock_ver, _mock_is_up, _mock_find, mock_popen):
        """新启动 Chrome 后身份核验失败时，绝不写入有效会话记录，并安全终止进程。"""
        mock_proc = MagicMock()
        mock_proc.pid = 8888
        mock_popen.return_value = mock_proc

        from jobagent.browser import launch
        with self.assertRaises(RuntimeError) as exc_info:
            launch(self.user_data_dir, port=9222, wait=0.6)

        self.assertIn("身份核验未通过", str(exc_info.exception))
        # 严格验证：会话文件绝对未被创建/写入！
        self.assertFalse(self.session_file.exists())
        # 严格验证：进程被安全终止
        mock_proc.terminate.assert_called()

    def test_get_managed_session_corrupt_or_missing(self):
        """会话文件不存在或损坏时返回 None。"""
        # 文件不存在
        res, reason = get_managed_session_with_reason(self.user_data_dir)
        self.assertIsNone(res)
        self.assertIn("不存在", reason)

        # 文件损坏
        self.session_file.write_text("{corrupt-json", encoding="utf-8")
        res2, reason2 = get_managed_session_with_reason(self.user_data_dir)
        self.assertIsNone(res2)
        self.assertIn("损坏", reason2)

    @patch("jobagent.browser.get_managed_session", return_value=None)
    def test_chat_listener_refuses_unverified_connection(self, _mock_managed):
        """ChatListener 在无法核验专属 Chrome 身份时拒绝启动并安全提示。"""
        listener = ChatListener(verbose=False)
        with self.assertRaises(RuntimeError) as exc_info:
            listener.connect()
        self.assertIn("未检测到受项目托管的专属求职 Chrome", str(exc_info.exception))


if __name__ == "__main__":
    unittest.main()
