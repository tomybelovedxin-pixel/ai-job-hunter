"""全天候会话监听与附件简历请求安全处理守护进程。"""

from __future__ import annotations

import time
from typing import Any, Callable
from pathlib import Path

from . import browser, cdp
from .adapters import boss
from .adapters.boss import BossAdapter, compute_card_fingerprint
from .config import Config, load as load_config
from .store import Store


class ChatListener:
    def __init__(
        self,
        config: Config | None = None,
        store: Store | None = None,
        *,
        adapter: BossAdapter | None = None,
        auto_accept: bool | None = None,
        interval: float = 6.0,
        max_rounds: int | None = None,
        verbose: bool = True,
    ):
        self.config = config or load_config()
        self.store = store or Store(self.config.db_path)
        self.adapter = adapter
        self.auto_accept = (
            auto_accept if auto_accept is not None else self.config.auto_accept_resume
        )
        self.interval = interval
        self.max_rounds = max_rounds
        self.verbose = verbose
        self.session: cdp.CDP | None = None
        self.unverified_or_processed: set[str] = set()

    def log(self, message: str) -> None:
        if self.verbose:
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] [监听守护] {message}", flush=True)

    def connect(self) -> None:
        # 严格核验本项目托管的 Chrome 实例身份，防止误连日常浏览器
        managed = browser.get_managed_session(self.config.browser_user_data_dir)
        if not managed:
            raise RuntimeError(
                "未检测到受项目托管的专属求职 Chrome 实例，或会话身份核验未通过！\n"
                "安全机制已阻断连接，防止附着到无关浏览器或日常个人浏览器。\n"
                "请运行 run_windows.bat 选项 1 或执行 python -m jobagent browser 启动专属求职浏览器。"
            )
        port = int(managed["port"])
        self.session = cdp.attach(port, url_contains="zhipin.com")
        self.adapter = BossAdapter(
            self.session, dump_dir=self.config.dump_dir, verbose=self.verbose
        )

    def step(self) -> dict[str, Any]:
        """执行单轮扫描核查：
        1. 检查风控与登录态
        2. 读取当前会话
        3. 检查是否有简历请求卡片
        4. 若开启 auto_accept 则安全执行同意并记录，否则提示
        """
        if not self.adapter:
            self.connect()
        assert self.adapter is not None

        block = self.adapter.check_block()
        if block:
            self.log(f"检测到页面风控/登录状态: {block}")
            return {"ok": False, "status": "blocked", "message": block}

        history = self.adapter.conversation_history()
        if not history.get("pane"):
            return {"ok": True, "status": "idle", "message": "当前未停留在有效聊天面板"}

        company = history.get("company", "")
        name = history.get("name", "")
        job = history.get("job", "")

        # 扫描简历请求
        detect_res = self.adapter.detect_resume_request(company=company, hr_name=name)
        cards = detect_res.get("cards", []) if detect_res.get("ok") else []
        event: dict[str, Any] = {
            "ok": True,
            "status": "normal",
            "company": company,
            "hr_name": name,
            "job": job,
            "cards_count": len(cards),
        }

        if cards:
            latest = cards[-1]
            fingerprint = latest.get("fingerprint") or boss.compute_card_fingerprint(
                company, name, latest.get("snippet", "")
            )
            event["fingerprint"] = fingerprint

            if latest.get("is_already_accepted"):
                event["resume_status"] = "already_accepted"
            elif latest.get("has_agree_button"):
                # 1. 跨进程持久化防重核验（查阅本地独立 SQLite 数据库）
                db_record = self.store.get_resume_request(fingerprint)
                if db_record and db_record.get("status") in ("accepted", "clicked-unverified"):
                    rec_status = db_record.get("status")
                    event["resume_status"] = "already_attempted"
                    event["db_status"] = rec_status
                    self.log(
                        f"卡片【{company} - {name}】在 SQLite 库中已有记录 (状态: {rec_status})，"
                        f"跨进程持久化防重生效，禁止重复操作"
                    )
                # 2. 内存防重核验
                elif fingerprint in self.unverified_or_processed:
                    event["resume_status"] = "already_attempted"
                    self.log(f"卡片【{company} - {name}】在本次运行中已尝试或处于未核实状态，禁止重复点击")
                else:
                    event["resume_status"] = "pending_request"
                    event["button_text"] = latest.get("agree_button_text")

                    if self.auto_accept:
                        self.log(f"检测到 HR「{name}」发起简历附件请求 (指纹: {fingerprint[:8]})，正在执行安全同意...")
                        self.unverified_or_processed.add(fingerprint)
                        action_res = self.adapter.accept_resume_request(
                            company=company, hr_name=name, dry_run=False
                        )
                        event["action_result"] = action_res
                        res_status = action_res.get("status", "unknown")
                        verified = bool(action_res.get("verified", False))
                        note = action_res.get("note", "")

                        # 持久化落库记录（保障守护进程重启后依然防重）
                        self.store.record_resume_request(
                            fingerprint=fingerprint,
                            company=company,
                            hr_name=name,
                            job=job,
                            snippet=latest.get("snippet", ""),
                            status=res_status,
                            verified=verified,
                            note=note,
                        )

                        if verified:
                            self.log(f"已成功同意并发送附件简历给 {company} - {name}！(指纹: {fingerprint[:8]})")
                            self.store.log_event(
                                "accept_resume",
                                company,
                                f"自动同意附件简历: {name} ({job}), 指纹: {fingerprint[:8]}, 校验通过",
                            )
                        else:
                            self.log(f"点击完成但状态待确认: {note} (已持久化记录，严禁重复点击)")
                    else:
                        self.log(
                            f"提示：检测到 HR「{name}」索要附件简历卡片 (指纹: {fingerprint[:8]})！"
                            f"(当前 auto_accept_resume=false，已保持安全只读，可在 config.toml 中开启)"
                        )
        return event

    def run_forever(
        self,
        interval: float = 6.0,
        max_iterations: int | None = None,
        callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.log(f"会话监听守护已就绪 (轮询间隔: {interval}s, 自动同意: {self.auto_accept})")
        self.log("提示：请在已打开的 Chrome 中保持 BOSS 聊天窗口打开...")
        count = 0
        try:
            while True:
                res = self.step()
                if callback:
                    callback(res)
                count += 1
                if max_iterations and count >= max_iterations:
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            self.log("监听守护进程已收到停止指令退出。")
