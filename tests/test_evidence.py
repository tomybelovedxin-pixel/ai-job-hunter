import unittest
from pathlib import Path
from jobagent.evidence import (
    CheckResult,
    _verified_grade,
    _is_safe_script_path,
    _check,
    ROOT,
)


class TestEvidence(unittest.TestCase):
    def test_verified_grade_no_illegal_upgrade(self):
        # 场景 1：声称 A 级，全部硬核证据通过 -> 维持 A
        checks_pass = [CheckResult("path", "file.py", True), CheckResult("run", "test.py", True)]
        self.assertEqual(_verified_grade(checks_pass, "A"), "A")

        # 场景 2：声称 A 级，部分通过 -> 降为 B
        checks_partial = [CheckResult("path", "file.py", True), CheckResult("run", "test.py", False)]
        self.assertEqual(_verified_grade(checks_partial, "A"), "B")

        # 场景 3：声称 A 级，全部失败 -> 降为 D
        checks_fail = [CheckResult("path", "file.py", False), CheckResult("run", "test.py", False)]
        self.assertEqual(_verified_grade(checks_fail, "A"), "D")

        # 场景 4：声称 C 级（仅调研/设计记录），即使硬证据全过，也绝不可升格为 A 或 B！
        self.assertEqual(_verified_grade(checks_pass, "C"), "C")

        # 场景 5：声称 D 级（仅自述），即使硬证据全过，也绝不可升格
        self.assertEqual(_verified_grade(checks_pass, "D"), "D")

        # 场景 6：仅有 statement（自述），硬核证据为空 -> 坚决给 D
        checks_statement_only = [CheckResult("statement", "我精通某某框架", True)]
        self.assertEqual(_verified_grade(checks_statement_only, "A"), "D")

    def test_safe_script_path_check(self):
        # 测试白名单安全检查：仅允许 tests 和 tools 目录下的脚本
        safe_test_script = (ROOT / "tests" / "test_demo.py").resolve()
        self.assertTrue(_is_safe_script_path(safe_test_script))

        safe_tool_script = (ROOT / "tools" / "run_eval.py").resolve()
        self.assertTrue(_is_safe_script_path(safe_tool_script))

        # 外部恶意脚本路径注入
        evil_script = (ROOT.parent / "evil.py").resolve()
        self.assertFalse(_is_safe_script_path(evil_script))

    def test_scheduled_task_injection_defense(self):
        # 包含特殊字符 / 命令注入 payload 的任务名必须被立即拦截
        res = _check("scheduled_task", "my_task; Remove-Item C:\\ -Recurse")
        self.assertFalse(res.ok)
        self.assertIn("非法字符", res.detail)

        res2 = _check("scheduled_task", "task' OR '1'='1")
        self.assertFalse(res2.ok)
        self.assertIn("非法字符", res2.detail)


if __name__ == "__main__":
    unittest.main()
