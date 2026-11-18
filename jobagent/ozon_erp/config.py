"""配置：真实凭据从环境变量或本机 config.toml 的 [ozon] 段读，不写进仓库。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ROOT

DEFAULT_BASE_URL = "https://api-seller.ozon.ru"


@dataclass
class OzonSettings:
    client_id: str = ""
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    db_path: Path = field(default_factory=lambda: ROOT / "data" / "ozon.sqlite")
    # 限流：Ozon 对卖家接口有频次限制，默认保守一点（每秒 8 次、并发 1）
    rate_per_second: float = 8.0
    max_retries: int = 4
    timeout: float = 30.0
    # 订单同步回看的重叠窗口（分钟）：防止边界上的单被漏掉
    overlap_minutes: int = 30
    mock: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.api_key)

    def describe(self) -> str:
        state = "已配置 Client-Id/Api-Key" if self.configured else "未配置凭据（只能跑 --mock）"
        return (f"{state}｜base_url={self.base_url}｜库={self.db_path}｜"
                f"限流 {self.rate_per_second}/秒｜重试 {self.max_retries} 次")


def load(config_path: Path | None = None) -> OzonSettings:
    """优先环境变量 OZON_CLIENT_ID / OZON_API_KEY，其次 config.toml 的 [ozon]。"""
    settings = OzonSettings()
    path = Path(config_path) if config_path else ROOT / "config.toml"
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            section = data.get("ozon", {}) or {}
            settings.client_id = str(section.get("client_id", "") or "")
            settings.api_key = str(section.get("api_key", "") or "")
            settings.base_url = str(section.get("base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL)
            if section.get("db"):
                settings.db_path = ROOT / str(section["db"])
            settings.rate_per_second = float(section.get("rate_per_second", settings.rate_per_second))
            settings.max_retries = int(section.get("max_retries", settings.max_retries))
            settings.timeout = float(section.get("timeout", settings.timeout))
        except (OSError, tomllib.TOMLDecodeError, ValueError):
            pass
    settings.client_id = os.environ.get("OZON_CLIENT_ID", settings.client_id)
    settings.api_key = os.environ.get("OZON_API_KEY", settings.api_key)
    if os.environ.get("OZON_BASE_URL"):
        settings.base_url = os.environ["OZON_BASE_URL"]
    return settings
