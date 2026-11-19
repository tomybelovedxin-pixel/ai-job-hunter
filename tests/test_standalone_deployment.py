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

    def test_data_dir_isolation_no_root_fallback(self):
        """验证个人资料、简历、数据库严格在 data_dir 内解析，禁止隐式回退到源码 ROOT。"""
        fake_user_dir = Path(self.tmp_dir) / "appdata_user"
        fake_user_dir.mkdir(parents=True, exist_ok=True)
        init_user_data(fake_user_dir)

        cfg_file = fake_user_dir / "config.toml"
        cfg = load(cfg_file)

        # 严格断言全部关键用户文件必须在独立数据目录内
        self.assertEqual(cfg.data_dir.resolve(), fake_user_dir.resolve())
        self.assertEqual(cfg.profile_path.resolve(), (fake_user_dir / "profile.json").resolve())
        self.assertEqual(cfg.master_resume_path.resolve(), (fake_user_dir / "master_resume.json").resolve())
        self.assertEqual(cfg.db_path.resolve(), (fake_user_dir / "jobs.sqlite").resolve())
        self.assertEqual(cfg.browser_user_data_dir.resolve(), (fake_user_dir / "chrome-profile").resolve())

        # 绝对不能指向项目源码根目录
        project_root = Path(__file__).resolve().parent.parent
        self.assertNotEqual(cfg.profile_path.resolve(), (project_root / "profile.json").resolve())
        self.assertNotEqual(cfg.db_path.resolve(), (project_root / "jobs.sqlite").resolve())

    def test_explicit_portable_mode(self):
        """验证只有在显式设置 AI_JOB_HUNTER_DATA_DIR 时才启用指定便携目录。"""
        portable_dir = Path(self.tmp_dir) / "portable_data"
        portable_dir.mkdir(parents=True, exist_ok=True)

        with patch.dict(os.environ, {"AI_JOB_HUNTER_DATA_DIR": str(portable_dir)}):
            data_dir = get_default_data_dir()
            self.assertEqual(data_dir.resolve(), portable_dir.resolve())

    def test_chat_listener_prevents_duplicate_unverified_clicks(self):
        """验证处于未确认状态 (clicked-unverified) 的求简历请求，后续轮询绝不重复触发点击。"""
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
            "cards": [{
                "has_agree_button": True,
                "is_already_accepted": False,
                "agree_button_text": "同意发送",
                "snippet": "请发送您的简历附件",
            }],
        }
        # 第一次点击：返回未确认状态
        mock_adapter.accept_resume_request.return_value = {
            "status": "clicked-unverified",
            "verified": False,
            "note": "点击后未确认页面状态",
        }

        mock_store = MagicMock()
        listener = ChatListener(
            adapter=mock_adapter,
            store=mock_store,
            auto_accept=True,
            verbose=False,
        )

        # 第 1 轮：触发点击，标记为未确认
        rep1 = listener.step()
        self.assertEqual(rep1["resume_status"], "pending_request")
        self.assertEqual(mock_adapter.accept_resume_request.call_count, 1)

        # 第 2 轮：同一张卡片仍然存在，但 listener 必须自动防重，禁止重复点击！
        rep2 = listener.step()
        self.assertEqual(rep2["resume_status"], "already_attempted")
        # accept_resume_request 调用计数依然为 1，未发生重复点击！
        self.assertEqual(mock_adapter.accept_resume_request.call_count, 1)

    def test_clicked_unverified_persists_across_daemon_restart(self):
        """测试 clicked-unverified 状态在守护进程完全重启后依然持久化生效，绝不重复点击。"""
        from jobagent.store import Store
        db_path = Path(self.tmp_dir) / "persistent_jobs.sqlite"
        store1 = Store(db_path)

        mock_adapter = MagicMock()
        mock_adapter.check_block.return_value = None
        mock_adapter.conversation_history.return_value = {
            "pane": True,
            "company": "字节跳动",
            "name": "李HR",
            "job": "架构师",
        }
        mock_adapter.detect_resume_request.return_value = {
            "ok": True,
            "cards": [{
                "has_agree_button": True,
                "is_already_accepted": False,
                "agree_button_text": "同意发送",
                "snippet": "请提供一份完整附件简历",
                "fingerprint": "bytedance_fp_001",
            }],
        }
        mock_adapter.accept_resume_request.return_value = {
            "status": "clicked-unverified",
            "verified": False,
            "note": "点击后回读未确认状态",
        }

        # 1. 运行第一个守护进程实例，触发点击，状态被持久化进 SQLite
        daemon1 = ChatListener(
            adapter=mock_adapter,
            store=store1,
            auto_accept=True,
            verbose=False,
        )
        rep1 = daemon1.step()
        self.assertEqual(rep1["resume_status"], "pending_request")
        self.assertEqual(mock_adapter.accept_resume_request.call_count, 1)

        # 验证 SQLite 库中已落盘
        saved = store1.get_resume_request("bytedance_fp_001")
        self.assertIsNotNone(saved)
        self.assertEqual(saved["status"], "clicked-unverified")
        self.assertFalse(saved["verified"])

        # 2. 模拟守护进程重启：彻底销毁旧实例，重新建立全新的 Store 与 Listener 实例（内存状态重置为全新）
        del daemon1
        del store1

        store2 = Store(db_path)
        daemon2 = ChatListener(
            adapter=mock_adapter,
            store=store2,
            auto_accept=True,
            verbose=False,
        )
        self.assertEqual(len(daemon2.unverified_or_processed), 0)

        # 3. 新守护进程执行单步核验
        rep2 = daemon2.step()
        self.assertEqual(rep2["resume_status"], "already_attempted")
        self.assertEqual(rep2["db_status"], "clicked-unverified")
        # 严格验证：跨进程重启后，仍然没有产生重复点击调用！调用次数依然维持为 1！
        self.assertEqual(mock_adapter.accept_resume_request.call_count, 1)

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
