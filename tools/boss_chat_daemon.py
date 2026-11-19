#!/usr/bin/env python3
"""BOSS 直聘实时聊天监听与附件简历请求自动处理守护脚本。

使用说明：
  python tools/boss_chat_daemon.py [--interval 6] [--auto-accept]

功能：
  1. 周期性（默认 6 秒）扫描当前聊天界面中的最新消息与卡片；
  2. 捕获 HR 索要简历附件卡片；
  3. 若开启自动同意且安全门禁全部通过，自动点击同意并回读校验；
  4. 遇到验证码或异常结构立即自动熔断停止，保存快照。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobagent.config import load as load_config
from jobagent.listener import ChatListener


def main() -> int:
    parser = argparse.ArgumentParser(description="BOSS 直聘聊天监听与简历自动同意守护进程")
    parser.add_argument("--interval", type=float, default=6.0, help="扫描轮询间隔秒数（默认 6.0）")
    parser.add_argument("--auto-accept", action="store_true", help="显式开启简历附件自动同意")
    parser.add_argument("--config", help="指定 config.toml 路径")
    args = parser.parse_args()

    cfg = load_config(args.config)
    auto_agree = True if args.auto_accept else cfg.auto_accept_resume

    listener = ChatListener(config=cfg, auto_accept=auto_agree, verbose=True)
    try:
        listener.run_forever(interval=args.interval)
        return 0
    except KeyboardInterrupt:
        print("\n[退出] 守护进程已停止。")
        return 0
    except Exception as exc:
        print(f"\n[错误] 守护异常退出: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
