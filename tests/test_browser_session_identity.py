import unittest
import json
from unittest.mock import MagicMock
from jobagent.adapters.boss import BossAdapter
from jobagent.adapters.base import BlockedError, AdapterError


class TestBrowserSessionIdentity(unittest.TestCase):
    def test_current_account_info_detection(self):
        mock_session = MagicMock()
        adapter = BossAdapter(mock_session, verbose=False)

        # 模拟页面存在已登录用户
        mock_session.evaluate.return_value = json.dumps({
            "logged_in": True,
            "user_name": "求职测试者"
        })
        info = adapter.current_account_info()
        self.assertTrue(info["logged_in"])
        self.assertEqual(info["user_name"], "求职测试者")

        # 模拟未登录状态
        mock_session.evaluate.return_value = json.dumps({
            "logged_in": False,
            "user_name": ""
        })
        info_unlogin = adapter.current_account_info()
        self.assertFalse(info_unlogin["logged_in"])

    def test_conversation_identity_and_switching(self):
        mock_session = MagicMock()
        adapter = BossAdapter(mock_session, verbose=False)
        adapter.check_block = MagicMock(return_value=None)
        adapter.dump_html = MagicMock(return_value="mock.html")

        # 场景：当前打开的是“公司A”的聊天，但任务目标是“公司B”
        adapter.conversation_history = MagicMock(side_effect=[
            {"pane": True, "company": "错误公司A", "name": "HR李", "job": "前端"},
            {"pane": True, "company": "正确公司B", "name": "HR王", "job": "Python"},
        ])
        adapter.open_conversation = MagicMock(return_value="已选中：正确公司B")
        adapter.detect_resume_request = MagicMock(return_value={"ok": True, "cards": []})

        res = adapter.accept_resume_request("正确公司B", dry_run=True)
        # 确认 adapter 触发了会话重新检索和点选，未盲目对错误公司操作
        adapter.open_conversation.assert_called_with("正确公司B")
        self.assertEqual(res["status"], "no_request")


if __name__ == "__main__":
    unittest.main()
