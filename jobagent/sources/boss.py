"""BOSS 直聘：走浏览器适配器，不在这里抓。

BOSS 的列表和详情必须登录才可见，站方也明确限制自动化访问。
所以这个"数据源"只登记状态，不做取数；要抓的时候用
`python -m jobagent scan`，它会连你自己登录过的调试浏览器。
"""

from __future__ import annotations

from .base import Filters, Source, SourceError, SourceResult


class BossSource(Source):
    name = "boss"
    label = "BOSS 直聘（浏览器登录）"
    homepage = "https://www.zhipin.com"
    notes = ("需要你先在调试浏览器里手工登录；由 jobagent/adapters/boss.py "
             "通过 CDP 只读取数，不注入凭据、不绕验证码")
    requires_login = True
    city_scoped = True

    def fetch(self, filters: Filters) -> SourceResult:
        raise SourceError(
            "BOSS 需要登录态，不能在无界面模式取数。"
            "请先 `python -m jobagent launch` 起调试浏览器并手工登录，"
            "再跑 `python -m jobagent scan`；或者把 JD 复制下来用 "
            "`python -m jobagent import <文件>` 走手工导入。"
        )
