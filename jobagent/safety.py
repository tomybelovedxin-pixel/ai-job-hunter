"""安全边界：需要你确认的动作一律先拦下来，并留下审计记录。"""

from __future__ import annotations

from typing import Any

REQUIRES_CONFIRMATION = {
    "send_message": "向招聘方发送消息或打招呼",
    "submit_application": "提交简历或申请",
    "upload_resume": "上传简历到平台",
    "accept_offer": "接受录用或合同",
    "fill_sensitive_profile": "填写身份证、银行等敏感资料",
    "enable_autosend": "开启自动发送",
    "change_global_config": "修改本机 Codex 全局配置",
    "install_external_app": "安装独立应用程序",
}


class NeedsConfirmation(RuntimeError):
    """动作需要用户确认，未确认前不执行。"""

    def __init__(self, action: str, detail: str = ""):
        description = REQUIRES_CONFIRMATION.get(action, action)
        extra = f"说明：{detail}" if detail else ""
        super().__init__(
            f"「{description}」属于需要你确认的动作，未经确认不会执行。{extra}"
            "确认方式：命令行加 --confirm，或在管理面板里点确认；"
            "每次确认都会写进数据库的 events 表。"
        )
        self.action = action


def assert_confirmed(store: Any, action: str, *, confirmed: bool, detail: str = "",
                     subject: str = "") -> None:
    """需要确认的动作先过这道闸。不需要确认的动作也记一笔审计。"""
    if action not in REQUIRES_CONFIRMATION:
        if store is not None:
            store.log_event("action", subject or action, detail or "无需确认的动作")
        return
    if not confirmed:
        if store is not None:
            store.log_event("blocked", subject or action,
                            f"未确认已拦下：{REQUIRES_CONFIRMATION[action]}")
        raise NeedsConfirmation(action, detail)
    if store is not None:
        store.log_event("confirmed", subject or action,
                        f"{REQUIRES_CONFIRMATION[action]}；{detail}")


def recent_confirmations(store: Any, limit: int = 20) -> list[Any]:
    return list(store.conn.execute(
        "SELECT * FROM events WHERE kind IN ('confirmed','blocked') ORDER BY id DESC LIMIT ?",
        (limit,),
    ))
