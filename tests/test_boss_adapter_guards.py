import unittest
from unittest.mock import MagicMock
from jobagent.adapters.boss import BossAdapter
from jobagent.adapters.base import AdapterError
from jobagent import guards


class TestBossAdapterGuards(unittest.TestCase):
    def test_boss_adapter_refuses_send_when_pane_unreadable(self):
        mock_session = MagicMock()
        mock_session.wait_for.return_value = True

        adapter = BossAdapter(mock_session, verbose=False)
        adapter.conversation_history = MagicMock(return_value={"pane": ""})
        adapter.dump_html = MagicMock(return_value="mock_dump.html")

        with self.assertRaises(AdapterError) as exc_info:
            adapter.send_text("你好，看到贵司岗位非常契合")

        self.assertIn("读取当前会话内容失败", str(exc_info.exception))
        self.assertIn("防止误发已拒绝发送", str(exc_info.exception))

    def test_guards_decide_send_deduplication(self):
        history = {
            "pane": "您好，之前关注过贵司的自动化项目，我们之前做过类似的爬虫和流式处理",
            "mine": ["您好，之前关注过贵司的自动化项目，我们之前做过类似的爬虫和流式处理"],
        }
        candidate_text = "您好，之前关注过贵司的自动化项目，我们之前做过类似的爬虫和流式处理"

        decision = guards.decide_send(history, candidate_text)
        self.assertEqual(decision["action"], "skip")
        self.assertTrue("重" in decision["reason"] or "已发" in decision["reason"])


if __name__ == "__main__":
    unittest.main()
