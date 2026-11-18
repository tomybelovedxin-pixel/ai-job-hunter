"""站点适配器：每个平台一个模块，对外接口一致。"""

from .base import Adapter, AdapterError, BlockedError

__all__ = ["Adapter", "AdapterError", "BlockedError"]
