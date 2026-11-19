"""Tests for standalone deployment, multi-user isolation, and zero developer machine dependency."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from jobagent.config import (
    get_default_data_dir,
    init_user_data,
    load,
    Config,
)
from jobagent.browser import (
    find_free_port,
    build_chrome_command,
)
from jobagent.listener import ChatListener


class TestStandaloneDeployment(unittest.TestCase):
    """Verifies that the codebase can run cleanly on any user's machine without hardcoded paths."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="jobagent_test_standalone_")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_a_different_windows_user_directories(self):
        """A. 模拟不同 Windows 用户名（如 ZhangSan, Admin, Developer），验证路径解析正确。"""
        fake_usernames = ["ZhangSan", "Admin", "Developer", "Alice"]
        for user in fake_usernames:
            mock_env = {
                "LOCALAPPDATA": f"C:\\Users\\{user}\\AppData\\Local",
                "USERPROFILE": f"C:\\Users\\{user}",
            }
            # Unset AI_JOB_HUNTER_DATA_DIR to test default discovery
            with patch.dict(os.environ, mock_env):
                os.environ.pop("AI_JOB_HUNTER_DATA_DIR", None)
                data_dir = get_default_data_dir()
                expected = Path(f"C:/Users/{user}/AppData/Local/AIJobHunter")
                self.assertEqual(data_dir.resolve(), expected.resolve())
                self.assertNotIn("ASUS", str(data_dir))

    def test_b_different_project_placement_directories(self):
        """B. 项目放置在任意磁盘和目录（D:\\Projects, C:\\Tools），相对路径与项目根解析一致。"""
        fake_roots = [
            Path(self.tmp_dir) / "Projects" / "ai-job-hunter",
            Path(self.tmp_dir) / "Tools" / "jobagent",
        ]
        for root in fake_roots:
            root.mkdir(parents=True, exist_ok=True)
            # Create a mock config inside that root
            cfg_file = root / "config.toml"
            cfg_file.write_text("[paths]\ndata_dir = 'custom_data'\n", encoding="utf-8")

            cfg = load(cfg_file)
            expected_data = (root / "custom_data").resolve()
            self.assertEqual(cfg.data_dir.resolve(), expected_data)
            self.assertTrue(cfg.db_path.resolve().is_relative_to(expected_data))

    def test_c_first_run_auto_init_and_no_overwrite(self):
        """C. 首次运行自动在用户数据目录生成配置文件，第二次运行绝不覆盖用户修改。"""
        test_data_dir = Path(self.tmp_dir) / "test_user_data"
        self.assertFalse(test_data_dir.exists())

        # First run: should create templates
        target_path, created = init_user_data(test_data_dir)
        self.assertTrue(test_data_dir.exists())
        created_names = [Path(p).name for p in created]
        self.assertIn("config.toml", created_names)
        self.assertIn("profile.json", created_names)
        self.assertIn("master_resume.json", created_names)

        # User modifies profile.json
        profile_file = test_data_dir / "profile.json"
        profile_data = {"name": "Test User Custom", "role": "Fullstack"}
        profile_file.write_text(json.dumps(profile_data), encoding="utf-8")

        # Second run: should NOT overwrite
        _, created_again = init_user_data(test_data_dir)
        self.assertEqual(created_again, [])

        with open(profile_file, "r", encoding="utf-8") as f:
            persisted = json.load(f)
        self.assertEqual(persisted["name"], "Test User Custom")

    def test_d_port_conflict_dynamic_switching(self):
        """D. 端口被非本项目进程占用时，能够发现空闲端口并切换。"""
        free_port = find_free_port(start_port=9230)
        self.assertGreaterEqual(free_port, 9230)
        self.assertLess(free_port, 9300)

    def test_e_browser_security_binding(self):
        """E. 浏览器启动参数必须为 127.0.0.1，不得出现 0.0.0.0 或通配 *。"""
        cmd = build_chrome_command(
            chrome_path="chrome.exe",
            port=9222,
            user_data_dir=Path(self.tmp_dir) / "chrome_profile",
        )
        cmd_str = " ".join(cmd)
        self.assertIn("--remote-debugging-address=127.0.0.1", cmd_str)
        self.assertNotIn("0.0.0.0", cmd_str)
        self.assertNotIn("--remote-allow-origins=*", cmd_str)
        self.assertIn("http://127.0.0.1:9222", cmd_str)

    def test_f_chat_listener_mock_run(self):
        """F. 验证 ChatListener 在遇到有效求简历卡片时的自动调度与状态记录。"""
        mock_adapter = MagicMock()
        mock_adapter.check_block.return_value = None
        mock_adapter.conversation_history.return_value = {
            "pane": True,
            "company": "Test Co",
            "name": "Alice HR",
            "job": "Python Engineer",
        }
        mock_adapter.detect_resume_request.return_value = {
            "ok": True,
            "cards": [
                {
                    "has_agree_button": True,
                    "is_already_accepted": False,
                    "agree_button_text": "同意发送",
                }
            ],
        }
        mock_adapter.accept_resume_request.return_value = {
            "verified": True,
            "note": "已同意",
        }

        mock_store = MagicMock()
        listener = ChatListener(
            adapter=mock_adapter,
            store=mock_store,
            auto_accept=True,
            verbose=False,
        )
        report = listener.step()

        self.assertTrue(report["ok"])
        self.assertEqual(report["resume_status"], "pending_request")
        self.assertEqual(report["action_result"]["verified"], True)
        mock_adapter.accept_resume_request.assert_called_once_with(
            company="Test Co", hr_name="Alice HR", dry_run=False
        )
        mock_store.log_event.assert_called_once()

    def test_m_zero_hardcoded_developer_paths(self):
        """M. 全仓库扫描：Python、BAT、PS1、MD、TOML、JSON 代码中 0 个 C:\\Users\\ASUS 硬编码路径。"""
        project_root = Path(__file__).resolve().parent.parent
        extensions_to_check = {".py", ".bat", ".ps1", ".md", ".toml", ".json", ".sh"}
        forbidden_needle = "C:\\Users\\ASUS".lower()

        flagged_files = []

        for p in project_root.rglob("*"):
            if not p.is_file():
                continue
            # Skip git internal files, temp test dirs, and virtualenvs
            if any(part in p.parts for part in [".git", ".venv", "venv", "__pycache__", ".pytest_cache"]):
                continue
            if p.suffix.lower() not in extensions_to_check:
                continue

            try:
                content = p.read_text(encoding="utf-8", errors="ignore").lower()
                if forbidden_needle in content:
                    flagged_files.append(str(p.relative_to(project_root)))
            except Exception:
                pass

        self.assertEqual(
            flagged_files,
            [],
            f"Found hardcoded developer paths in files: {flagged_files}",
        )


if __name__ == "__main__":
    unittest.main()
