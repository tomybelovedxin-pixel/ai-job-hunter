import unittest
from pathlib import Path
from jobagent.outreach import compose_first_contact
from jobagent.textquality import scan
from jobagent.profile import Profile


class TestOutreachQuality(unittest.TestCase):
    def test_textquality_audit_ai_cliche_filter(self):
        # 典型 AI 味大词套话 -> 应该被扫描出 issues
        bad_text = "总的来说，我能够全面赋能贵司业务并实现商业闭环，具备出色的沟通能力，欢迎随时联系我！"
        result = scan(bad_text)
        self.assertTrue(len(result["issues"]) > 0)

        # 自然口语化短话术
        good_text = "看到贵司在招 Python 爬虫，我之前用 CDP 适配过 BOSS 直聘抓取和去重，想请教下目前采集主要是反爬还是数据解析？"
        result_good = scan(good_text)
        self.assertEqual(len(result_good["issues"]), 0)
        self.assertTrue(50 <= len(good_text) <= 95)

    def test_compose_word_length_boundary(self):
        profile = Profile.load(Path("profile.example.json"))
        job = {
            "title": "Python 自动化开发",
            "company": "测试科技",
            "description": "负责自动化脚本、网页抓取与数据清洗，要求熟悉 Python 及 CDP 自动化。",
        }
        analysis = {"strengths": ["Python 自动化开发", "CDP 浏览器控制"], "gaps": []}
        res = compose_first_contact(job, analysis, profile=profile)
        greeting = res.get("text", "")
        self.assertTrue(len(greeting) <= 120, f"打招呼字数超标: {len(greeting)} 字 -> {greeting}")


if __name__ == "__main__":
    unittest.main()
