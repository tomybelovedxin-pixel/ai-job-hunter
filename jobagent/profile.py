"""候选人档案：从 profile.json 读能力项与红线，供打分和话术使用。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Capability:
    name: str
    level: int
    keywords: list[str] = field(default_factory=list)
    evidence: str = ""


@dataclass
class Profile:
    raw: dict[str, Any]
    path: Path

    @property
    def name(self) -> str:
        return self.raw.get("name", "")

    @property
    def headline(self) -> str:
        return self.raw.get("headline", "")

    @property
    def city(self) -> str:
        return self.raw.get("city", "")

    @property
    def remote_ok(self) -> bool:
        return bool(self.raw.get("remote_ok", True))

    @property
    def city_preferred(self) -> list[str]:
        return list(self.raw.get("city_preferred", []))

    @property
    def salary_floor(self) -> float:
        return float(self.raw.get("salary_floor", 0) or 0)

    @property
    def directions(self) -> list[dict[str, Any]]:
        return list(self.raw.get("directions", []))

    @property
    def capabilities(self) -> list[Capability]:
        return [
            Capability(
                name=item.get("name", ""),
                level=int(item.get("level", 0)),
                keywords=[str(k).lower() for k in item.get("keywords", [])],
                evidence=item.get("evidence", ""),
            )
            for item in self.raw.get("capabilities", [])
        ]

    @property
    def hard_blocks(self) -> list[str]:
        return [str(x).lower() for x in self.raw.get("hard_blocks", [])]

    @property
    def soft_blocks(self) -> list[dict[str, str]]:
        return [
            {"pattern": str(item.get("pattern", "")).lower(), "note": item.get("note", "")}
            for item in self.raw.get("soft_blocks", [])
        ]

    @property
    def questions(self) -> list[str]:
        return list(self.raw.get("questions", []))

    def capability(self, name: str) -> Capability | None:
        for cap in self.capabilities:
            if cap.name == name:
                return cap
        return None

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"找不到档案文件: {path}")
        return cls(raw=json.loads(path.read_text(encoding="utf-8")), path=path)
