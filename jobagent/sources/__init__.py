"""岗位数据源注册表。

每个源都写清楚：取数方式、是否要登录、有没有官方接口、怎么合规使用。
加新源只要在 SOURCE_CLASSES 里挂一个类，再在 config.toml 的
[sources].enabled 里打开即可，不用改流水线。
"""

from __future__ import annotations

from .baidu import BaiduSource
from .base import (Filters, Source, SourceError, SourceResult, city_allowed, http_json,
                   http_post_json)
from .boss import BossSource
from .eleduck import EleduckSource
from .himalayas import HimalayasSource
from .hnjobs import HackerNewsSource
from .jobicy import JobicySource
from .manual import ManualSource
from .remotive import RemotiveSource
from .remoteok import RemoteOKSource
from .tencent import TencentSource
from .workingnomads import WorkingNomadsSource

SOURCE_CLASSES: dict[str, type[Source]] = {
    TencentSource.name: TencentSource,
    BaiduSource.name: BaiduSource,
    RemoteOKSource.name: RemoteOKSource,
    JobicySource.name: JobicySource,
    RemotiveSource.name: RemotiveSource,
    HackerNewsSource.name: HackerNewsSource,
    EleduckSource.name: EleduckSource,
    WorkingNomadsSource.name: WorkingNomadsSource,
    HimalayasSource.name: HimalayasSource,
    BossSource.name: BossSource,
    ManualSource.name: ManualSource,
}

DEFAULT_ENABLED = ("tencent", "baidu", "eleduck", "remoteok", "jobicy", "remotive",
                   "workingnomads", "himalayas", "hn")


def get(name: str) -> Source:
    try:
        return SOURCE_CLASSES[name]()
    except KeyError:
        raise SourceError(f"没有这个数据源: {name}；可用的是 {', '.join(sorted(SOURCE_CLASSES))}")


def resolve(names: list[str] | None) -> list[Source]:
    selected = list(names or DEFAULT_ENABLED)
    sources: list[Source] = []
    for name in selected:
        source = get(name)
        if isinstance(source, ManualSource):
            # 手工导入需要具体文件或文本，不能空跑
            continue
        sources.append(source)
    return sources


def describe_all() -> list[dict[str, object]]:
    return [SOURCE_CLASSES[name]().describe() for name in SOURCE_CLASSES]


__all__ = [
    "Filters",
    "Source",
    "SourceError",
    "SourceResult",
    "SOURCE_CLASSES",
    "DEFAULT_ENABLED",
    "city_allowed",
    "http_json",
    "http_post_json",
    "get",
    "resolve",
    "describe_all",
]
