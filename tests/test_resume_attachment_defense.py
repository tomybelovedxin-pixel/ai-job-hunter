import unittest
from unittest.mock import MagicMock
from jobagent.adapters.boss import BossAdapter
from jobagent.adapters.base import AdapterError


class TestResumeAttachmentDefense(unittest.TestCase):
    def setUp(self):
        self.mock_session = MagicMock()
        self.adapter = BossAdapter(self.mock_session, verbose=False)
        self.adapter.check_block = MagicMock(return_value=None)
        self.adapter.dump_html = MagicMock(return_value="mock_dump.html")
        self.adapter.conversation_history = MagicMock(return_value={
            "pane": True,
            "company": "测试科技",
            "name": "HR李",
            "job": "Python开发"
        })

    def test_distinguish_plain_text_vs_structured_card(self):
        # 场景 1：HR 发送普通聊天消息 “你可以发我一份附件简历吗”，但未发起系统卡片
        self.adapter.detect_resume_request = MagicMock(return_value={
            "ok": True,
            "cards": []  # 无卡片
        })
        res = self.adapter.accept_resume_request("测试科技", dry_run=True)
        self.assertEqual(res["status"], "no_request")
        self.assertIn("普通聊天消息不触发自动同意", res["note"])

    def test_prevent_duplicate_accept(self):
        # 场景 2：HR 发起了卡片，但卡片状态已显示为“已同意/已发送”
        self.adapter.detect_resume_request = MagicMock(return_value={
            "ok": True,
            "cards": [{
                "snippet": "请求获取您的附件简历",
                "is_already_accepted": True,
                "has_agree_button": False,
                "agree_button_text": "",
            }]
        })
        res = self.adapter.accept_resume_request("测试科技", dry_run=False)
        self.assertEqual(res["status"], "already_accepted")
        self.assertTrue(res["verified"])
        self.assertIn("已自动防重跳过", res["note"])

    def test_prevent_clicking_wrong_reject_button(self):
        # 场景 3：卡片中只有“拒绝”按钮或页面结构变动导致同意按钮缺失 -> 严禁误点拒绝！
        self.adapter.detect_resume_request = MagicMock(return_value={
            "ok": True,
            "cards": [{
                "snippet": "求简历",
                "is_already_accepted": False,
                "has_agree_button": False,  # 只有拒绝按钮，未匹配到同意按钮
                "agree_button_text": "",
                "reject_button_present": True
            }]
        })
        with self.assertRaises(AdapterError) as exc_info:
            self.adapter.accept_resume_request("测试科技", dry_run=False)

        self.assertIn("未定位到安全同意按钮", str(exc_info.exception))

    def test_page_structure_change_failsafe(self):
        # 场景 4：页面结构变化，检测接口报错 -> 必须立即停止，拒绝盲目操作
        self.adapter.detect_resume_request = MagicMock(return_value={
            "ok": False,
            "message": "DOM 选择器失配"
        })
        with self.assertRaises(AdapterError) as exc_info:
            self.adapter.accept_resume_request("测试科技", dry_run=False)

        self.assertIn("检测简历请求卡片失败", str(exc_info.exception))

    def test_real_click_and_post_verification(self):
        # 场景 5：合规卡片点击与结果回读校验
        self.adapter.detect_resume_request = MagicMock(side_effect=[
            # 第一次检测：发现待同意卡片
            {
                "ok": True,
                "cards": [{
                    "snippet": "向您发起了简历附件请求",
                    "is_already_accepted": False,
                    "has_agree_button": True,
                    "agree_button_text": "同意发送",
                    "button_selector": "[data-jobagent-action='agree-0-0']"
                }]
            },
            # 第二次检测（点击后回读）：卡片状态已变成已同意
            {
                "ok": True,
                "cards": [{
                    "snippet": "已将附件简历发送给对方",
                    "is_already_accepted": True,
                    "has_agree_button": False,
                }]
            }
        ])
        self.adapter.click_expr = MagicMock(return_value={"clicked": True})

        res = self.adapter.accept_resume_request("测试科技", dry_run=False)
        self.assertEqual(res["status"], "accepted")
        self.assertTrue(res["verified"])
        self.assertEqual(res["button_text"], "同意发送")


if __name__ == "__main__":
    unittest.main()
