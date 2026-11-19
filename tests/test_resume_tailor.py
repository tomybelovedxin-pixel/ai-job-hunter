import unittest
from jobagent.resume.tailor import build_doc


class TestResumeTailor(unittest.TestCase):
    def test_resume_tailor_evidence_gate(self):
        # 模拟主简历数据
        master = {
            "basics": {
                "name": "测试员",
                "age": "25",
                "city": "北京",
                "headline": "Python 工程师",
            },
            "summary": {"default": "专业做自动化与工具开发"},
            "skill_groups": [
                {
                    "label": "AI 应用",
                    "items": "LLM API",
                    "claims": ["claim-ai"],
                },
                {
                    "label": "未验证技能",
                    "items": "Quantum Computing",
                    "claims": ["claim-unverified"],
                },
            ],
            "projects": [
                {
                    "id": "proj-verified",
                    "name": "已验证的真实项目",
                    "claims": ["claim-proj-1"],
                    "tech": ["Python"],
                    "bullets": {"default": ["完成自动化流水线搭建"]},
                },
                {
                    "id": "proj-no-claims",
                    "name": "无证据凭空虚构项目",
                    "claims": [],  # 无关联证据
                    "tech": ["Go"],
                    "bullets": {"default": ["自称完成了重大架构重构"]},
                },
                {
                    "id": "proj-failed-claims",
                    "name": "证据未通过的项目",
                    "claims": ["claim-failed"],
                    "tech": ["Rust"],
                    "bullets": {"default": ["自称开发了微服务"]},
                },
            ],
        }

        # 核验报告：仅 claim-proj-1 为 A 级，claim-ai 为 A 级，claim-failed 为 D 级
        report = {
            "claims": [
                {"id": "claim-proj-1", "verified_grade": "A"},
                {"id": "claim-ai", "verified_grade": "A"},
                {"id": "claim-failed", "verified_grade": "D"},
                {"id": "claim-unverified", "verified_grade": "D"},
            ]
        }

        doc = build_doc(master=master, report=report, job={"title": "Python 开发"})

        # 1. 只有具备 A/B 级证据的项目能进入裁剪后的正文
        project_names = [p["name"] for p in doc.projects]
        self.assertIn("已验证的真实项目", project_names)
        self.assertNotIn("无证据凭空虚构项目", project_names)
        self.assertNotIn("证据未通过的项目", project_names)

        # 2. 只有具备 A/B 级证据的技能组被保留
        group_labels = [g["label"] for g in doc.skill_groups]
        self.assertIn("AI 应用", group_labels)
        self.assertNotIn("未验证技能", group_labels)


if __name__ == "__main__":
    unittest.main()
