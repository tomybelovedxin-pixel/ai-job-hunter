"""Tests for resume attachment request detection, safety verification, and click defense."""
import json
import unittest
from unittest.mock import MagicMock, patch

from jobagent.adapters.base import AdapterError
from jobagent.adapters.boss import BossAdapter


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
            "job": "Python开发",
        })

    def test_click_selector_success(self):
        """测试点击函数（CSS选择器）成功。"""
        self.mock_session.evaluate.return_value = json.dumps({
            "ok": True,
            "clicked": True,
            "error": None,
        })
        res = self.adapter.click_selector("[data-jobagent-action='agree-0-0']")
        self.assertTrue(res["ok"])
        self.assertTrue(res["clicked"])
        self.assertIsNone(res["error"])

    def test_click_selector_failure(self):
        """测试点击函数（CSS选择器）失败（未找到元素）。"""
        self.mock_session.evaluate.return_value = json.dumps({
            "ok": False,
            "clicked": False,
            "error": "未找到匹配选择器的元素",
        })
        res = self.adapter.click_selector("[data-jobagent-action='missing-0-0']")
        self.assertFalse(res["ok"])
        self.assertFalse(res["clicked"])
        self.assertIn("未找到", res["error"])

    def test_click_selector_invalid_css(self):
        """测试 CSS 选择器无效时的容错防护。"""
        self.mock_session.evaluate.return_value = json.dumps({
            "ok": False,
            "clicked": False,
            "error": "CSS选择器语法无效",
        })
        res = self.adapter.click_selector("[invalid==selector]")
        self.assertFalse(res["ok"])
        self.assertFalse(res["clicked"])
        self.assertIn("语法无效", res["error"])

    def test_click_expr_success_and_failure(self):
        """测试 JavaScript 元素表达式点击函数成功与非Element返回。"""
        # 成功
        self.mock_session.evaluate.return_value = json.dumps({
            "ok": True,
            "clicked": True,
            "error": None,
        })
        res_ok = self.adapter.click_expr("document.querySelector('button')")
        self.assertTrue(res_ok["clicked"])

        # 失败（返回 null）
        self.mock_session.evaluate.return_value = json.dumps({
            "ok": False,
            "clicked": False,
            "error": "未找到目标元素 (null)",
        })
        res_fail = self.adapter.click_expr("null")
        self.assertFalse(res_fail["clicked"])

    def test_distinguish_plain_text_vs_structured_card(self):
        """HR 发送普通聊天消息 “你可以发我一份附件简历吗”，但未发起系统卡片 -> 不触发同意。"""
        self.adapter.detect_resume_request = MagicMock(return_value={
            "ok": True,
            "cards": [],
        })
        res = self.adapter.accept_resume_request("测试科技", dry_run=True)
        self.assertEqual(res["status"], "no_request")
        self.assertIn("普通聊天消息不触发自动同意", res["note"])

    def test_prevent_duplicate_accept(self):
        """同一个请求此前已经同意过 -> 自动防重跳过，不重复点击。"""
        self.adapter.detect_resume_request = MagicMock(return_value={
            "ok": True,
            "cards": [{
                "snippet": "请求获取您的附件简历",
                "is_already_accepted": True,
                "has_agree_button": False,
                "agree_button_text": "",
            }],
        })
        res = self.adapter.accept_resume_request("测试科技", dry_run=False)
        self.assertEqual(res["status"], "already_accepted")
        self.assertTrue(res["verified"])
        self.assertIn("已自动防重跳过", res["note"])

    def test_prevent_clicking_wrong_reject_button(self):
        """卡片中只有拒绝/关闭按钮或按钮文本包含拒绝 -> 严禁误点拒绝！"""
        self.adapter.detect_resume_request = MagicMock(return_value={
            "ok": True,
            "cards": [{
                "snippet": "求简历",
                "is_already_accepted": False,
                "has_agree_button": False,
                "agree_button_text": "",
                "reject_button_present": True,
            }],
        })
        with self.assertRaises(AdapterError) as exc_info:
            self.adapter.accept_resume_request("测试科技", dry_run=False)
        self.assertIn("未定位到安全同意按钮", str(exc_info.exception))

    def test_simultaneous_agree_and_reject_buttons(self):
        """卡片中同时存在同意和拒绝按钮 -> 严格只选中同意按钮，严禁触碰拒绝。"""
        self.adapter.detect_resume_request = MagicMock(return_value={
            "ok": True,
            "cards": [{
                "snippet": "希望获取您的完整简历",
                "is_already_accepted": False,
                "has_agree_button": True,
                "agree_button_text": "同意发送",
                "button_selector": "[data-jobagent-action='agree-0-0']",
                "reject_button_present": True,
            }],
        })
        res_dry = self.adapter.accept_resume_request("测试科技", dry_run=True)
        self.assertEqual(res_dry["status"], "dry-run")
        self.assertEqual(res_dry["button_text"], "同意发送")
        self.assertNotIn("拒绝", res_dry["button_text"])

    def test_card_disappears_before_click(self):
        """卡片在点击前二次核验时消失（例如页面异步刷新或被撤回）-> 立即终止。"""
        self.adapter.detect_resume_request = MagicMock(side_effect=[
            # 初始检测：有卡片
            {
                "ok": True,
                "cards": [{
                    "snippet": "求简历",
                    "is_already_accepted": False,
                    "has_agree_button": True,
                    "agree_button_text": "同意",
                    "button_selector": "[data-jobagent-action='agree-0-0']",
                }],
            },
            # 点击前 precheck：卡片消失
            {
                "ok": True,
                "cards": [],
            },
        ])
        with self.assertRaises(AdapterError) as exc_info:
            self.adapter.accept_resume_request("测试科技", dry_run=False)
        self.assertIn("简历请求卡片已在页面消失", str(exc_info.exception))

    def test_agree_button_disappears_before_click(self):
        """同意按钮在点击前二次核验时消失或失效 -> 立即终止。"""
        self.adapter.detect_resume_request = MagicMock(side_effect=[
            # 初始检测：有同意按钮
            {
                "ok": True,
                "cards": [{
                    "snippet": "求简历",
                    "is_already_accepted": False,
                    "has_agree_button": True,
                    "agree_button_text": "同意",
                    "button_selector": "[data-jobagent-action='agree-0-0']",
                }],
            },
            # 点击前 precheck：按钮消失
            {
                "ok": True,
                "cards": [{
                    "snippet": "求简历",
                    "is_already_accepted": False,
                    "has_agree_button": False,
                    "agree_button_text": "",
                }],
            },
        ])
        with self.assertRaises(AdapterError) as exc_info:
            self.adapter.accept_resume_request("测试科技", dry_run=False)
        self.assertIn("同意按钮已消失或失效", str(exc_info.exception))

    def test_page_structure_change_failsafe(self):
        """页面结构异常变动时立即停止，拒绝盲目操作。"""
        self.adapter.detect_resume_request = MagicMock(return_value={
            "ok": False,
            "message": "DOM 选择器失配",
        })
        with self.assertRaises(AdapterError) as exc_info:
            self.adapter.accept_resume_request("测试科技", dry_run=False)
        self.assertIn("检测简历请求卡片失败", str(exc_info.exception))

    @patch("time.sleep", return_value=None)
    def test_click_post_verification_agreed(self, _sleep):
        """点击后回读确认为已同意状态 -> 校验通过。"""
        self.adapter.detect_resume_request = MagicMock(side_effect=[
            # 1. 初始检测
            {
                "ok": True,
                "cards": [{
                    "snippet": "向您发起了简历附件请求",
                    "is_already_accepted": False,
                    "has_agree_button": True,
                    "agree_button_text": "同意发送",
                    "button_selector": "[data-jobagent-action='agree-0-0']",
                }],
            },
            # 2. 点击前 precheck
            {
                "ok": True,
                "cards": [{
                    "snippet": "向您发起了简历附件请求",
                    "is_already_accepted": False,
                    "has_agree_button": True,
                    "agree_button_text": "同意发送",
                    "button_selector": "[data-jobagent-action='agree-0-0']",
                }],
            },
            # 3. 点击后 post-verify 回读
            {
                "ok": True,
                "cards": [{
                    "snippet": "已将附件简历发送给对方",
                    "is_already_accepted": True,
                    "has_agree_button": False,
                }],
            },
        ])
        self.adapter.click_selector = MagicMock(return_value={"ok": True, "clicked": True, "error": None})

        res = self.adapter.accept_resume_request("测试科技", dry_run=False)
        self.assertEqual(res["status"], "accepted")
        self.assertTrue(res["verified"])
        self.assertEqual(res["button_text"], "同意发送")
        self.adapter.click_selector.assert_called_once_with("[data-jobagent-action='agree-0-0']")

    @patch("time.sleep", return_value=None)
    def test_click_post_verification_unverified(self, _sleep):
        """点击后状态无法确证更新 -> 标记 clicked-unverified，不重复点击。"""
        self.adapter.detect_resume_request = MagicMock(side_effect=[
            # 1. 初始检测
            {
                "ok": True,
                "cards": [{
                    "snippet": "向您发起了简历附件请求",
                    "is_already_accepted": False,
                    "has_agree_button": True,
                    "agree_button_text": "同意发送",
                    "button_selector": "[data-jobagent-action='agree-0-0']",
                }],
            },
            # 2. 点击前 precheck
            {
                "ok": True,
                "cards": [{
                    "snippet": "向您发起了简历附件请求",
                    "is_already_accepted": False,
                    "has_agree_button": True,
                    "agree_button_text": "同意发送",
                    "button_selector": "[data-jobagent-action='agree-0-0']",
                }],
            },
            # 3. 点击后 post-verify 回读：卡片依然是待处理状态（未更新）
            {
                "ok": True,
                "cards": [{
                    "snippet": "向您发起了简历附件请求",
                    "is_already_accepted": False,
                    "has_agree_button": True,
                    "agree_button_text": "同意发送",
                }],
            },
        ])
        self.adapter.click_selector = MagicMock(return_value={"ok": True, "clicked": True, "error": None})

        res = self.adapter.accept_resume_request("测试科技", dry_run=False)
        self.assertEqual(res["status"], "clicked-unverified")
        self.assertFalse(res["verified"])
        self.assertIn("未确认状态", res["note"])

    def test_card_identity_fingerprint_mismatch_rejected(self):
        """点击前卡片身份或指纹不匹配（例如卡片被替换或变动）-> 坚决拒绝点击并终止。"""
        self.adapter.detect_resume_request = MagicMock(side_effect=[
            # 1. 初始检测：指纹 A
            {
                "ok": True,
                "cards": [{
                    "snippet": "向您发起了简历附件请求 A",
                    "fingerprint": "fingerprint_card_A",
                    "is_already_accepted": False,
                    "has_agree_button": True,
                    "agree_button_text": "同意发送",
                    "button_selector": "[data-jobagent-action='agree-0-0']",
                }],
            },
            # 2. 点击前 precheck：返回不同卡片指纹 B（卡片漂移）
            {
                "ok": True,
                "cards": [{
                    "snippet": "向您发起了简历附件请求 B",
                    "fingerprint": "fingerprint_card_B",
                    "is_already_accepted": False,
                    "has_agree_button": True,
                    "agree_button_text": "同意发送",
                    "button_selector": "[data-jobagent-action='agree-0-0']",
                }],
            },
        ])
        with self.assertRaises(AdapterError) as exc_info:
            self.adapter.accept_resume_request("测试科技", dry_run=False)
        self.assertIn("卡片指纹不一致", str(exc_info.exception))

    def test_button_text_changed_before_click_rejected(self):
        """点击前按钮文案发生变动（变为非白名单或包含拒绝语义）-> 坚决终止。"""
        self.adapter.detect_resume_request = MagicMock(side_effect=[
            # 1. 初始检测：正常文案
            {
                "ok": True,
                "cards": [{
                    "snippet": "向您发起了简历附件请求",
                    "fingerprint": "card_fp_1",
                    "is_already_accepted": False,
                    "has_agree_button": True,
                    "agree_button_text": "同意发送",
                    "button_selector": "[data-jobagent-action='agree-0-0']",
                }],
            },
            # 2. 点击前 precheck：按钮突变包含拒绝
            {
                "ok": True,
                "cards": [{
                    "snippet": "向您发起了简历附件请求",
                    "fingerprint": "card_fp_1",
                    "is_already_accepted": False,
                    "has_agree_button": True,
                    "agree_button_text": "拒绝发送",
                    "button_selector": "[data-jobagent-action='agree-0-0']",
                }],
            },
        ])
        with self.assertRaises(AdapterError) as exc_info:
            self.adapter.accept_resume_request("测试科技", dry_run=False)
        self.assertIn("拒绝/取消", str(exc_info.exception))

    def test_hr_or_company_mismatch_rejected(self):
        """当前会话中的 HR 姓名与目标 HR 不匹配时拒绝操作。"""
        # 当前会话是 HR李，目标是 HR王
        with self.assertRaises(AdapterError) as exc_info:
            self.adapter.accept_resume_request("测试科技", hr_name="HR王", dry_run=True)
        self.assertIn("不匹配", str(exc_info.exception))


if __name__ == "__main__":
    unittest.main()
