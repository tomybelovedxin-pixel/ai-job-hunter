"""工具目录：登记每个候选开源项目/依赖的状态、证据和调用方式。

状态用 A–E 表示，含义写死在这里，避免"发现了仓库"被说成"已经集成"：
  A 已发现，尚未验证
  B 已评估，尚未安装
  C 已安装，尚未验证
  D 已验证，可以调用
  E 暂停使用或存在风险
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS tools (
    name            TEXT PRIMARY KEY,
    kind            TEXT,              -- 集成方式：技能/MCP/外部API/CLI/Python库/独立应用/仅参考
    purpose         TEXT,              -- 解决什么问题
    repo_url        TEXT,
    docs_url        TEXT,
    license         TEXT,
    version         TEXT,
    inputs          TEXT,
    outputs         TEXT,
    runtime         TEXT,              -- 运行环境与依赖
    entry           TEXT,              -- 调用入口
    permissions     TEXT,              -- 权限需求
    installed       INTEGER DEFAULT 0,
    verified        INTEGER DEFAULT 0,
    auto_call       INTEGER DEFAULT 0, -- 是否允许智能体自主调用
    state           TEXT NOT NULL DEFAULT 'A',
    risk            TEXT,
    failure_policy  TEXT,
    notes           TEXT,
    source          TEXT,              -- 谁发现的：discover / 人工 / 内置
    stars           INTEGER,
    last_commit     TEXT,
    last_checked_at TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,     -- discover/evaluate/install/verify/call/block
    ok         INTEGER,
    detail     TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_evidence (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,     -- 检查项
    value      TEXT,
    passed     INTEGER,
    created_at TEXT NOT NULL
);
"""

STATES = {
    "A": "已发现，尚未验证",
    "B": "已评估，尚未安装",
    "C": "已安装，尚未验证",
    "D": "已验证，可以调用",
    "E": "暂停使用或存在风险",
}

KINDS = ("技能/指令包", "MCP 服务", "外部 API", "CLI 工具", "Python 库", "独立应用", "仅参考")


@dataclass
class Tool:
    name: str
    kind: str = "Python 库"
    purpose: str = ""
    repo_url: str = ""
    docs_url: str = ""
    license: str = ""
    version: str = ""
    inputs: str = ""
    outputs: str = ""
    runtime: str = ""
    entry: str = ""
    permissions: str = ""
    installed: bool = False
    verified: bool = False
    auto_call: bool = False
    state: str = "A"
    risk: str = ""
    failure_policy: str = ""
    notes: str = ""
    source: str = "内置"
    stars: int | None = None
    last_commit: str = ""
    last_checked_at: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def state_label(self) -> str:
        return STATES.get(self.state, self.state)

    def as_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["state_label"] = STATES.get(self.state, self.state)
        return data


class Catalog:
    def __init__(self, conn: Any):
        self.conn = conn
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- 写入 ----------

    def upsert(self, tool: Tool | dict[str, Any], *, source: str | None = None) -> str:
        data = tool.as_dict() if isinstance(tool, Tool) else dict(tool)
        name = data.get("name")
        if not name:
            raise ValueError("工具必须有 name")
        existing = self.get(name)
        now = _now()
        fields = [
            "name", "kind", "purpose", "repo_url", "docs_url", "license", "version",
            "inputs", "outputs", "runtime", "entry", "permissions", "state", "risk",
            "failure_policy", "notes", "source", "stars", "last_commit", "last_checked_at",
        ]
        values = [data.get(field) for field in fields]
        values[0] = name
        values[fields.index("state")] = data.get("state") or "A"
        if source:
            values[fields.index("source")] = source
        flags = ["installed", "verified", "auto_call"]
        if existing:
            # 人工登记过的字段不被自动发现的空值覆盖
            sets = []
            params: list[Any] = []
            for field_name, value in zip(fields, values):
                if field_name in ("name", "state", "source") and value in (None, ""):
                    continue
                if field_name in ("purpose", "risk", "failure_policy", "notes",
                                  "permissions", "entry") and not value:
                    continue
                sets.append(f"{field_name}=COALESCE(?, {field_name})")
                params.append(value)
            for flag in flags:
                if data.get(flag) is not None:
                    sets.append(f"{flag}=?")
                    params.append(1 if data.get(flag) else 0)
            sets.append("updated_at=?")
            params.extend([now, name])
            self.conn.execute(f"UPDATE tools SET {', '.join(sets)} WHERE name=?", params)
        else:
            columns = fields + flags + ["created_at", "updated_at"]
            placeholders = ",".join("?" * len(columns))
            self.conn.execute(
                f"INSERT INTO tools ({','.join(columns)}) VALUES ({placeholders})",
                [*values,
                 1 if data.get("installed") else 0,
                 1 if data.get("verified") else 0,
                 1 if data.get("auto_call") else 0,
                 now, now],
            )
        self.conn.commit()
        return name

    def set_state(self, name: str, state: str, *, note: str = "",
                  installed: bool | None = None, verified: bool | None = None,
                  auto_call: bool | None = None, risk: str | None = None) -> None:
        if state not in STATES:
            raise ValueError(f"未知状态 {state}")
        sets = ["state=?", "updated_at=?"]
        params: list[Any] = [state, _now()]
        if installed is not None:
            sets.append("installed=?")
            params.append(1 if installed else 0)
        if verified is not None:
            sets.append("verified=?")
            params.append(1 if verified else 0)
        if auto_call is not None:
            sets.append("auto_call=?")
            params.append(1 if auto_call else 0)
        if risk is not None:
            sets.append("risk=?")
            params.append(risk)
        if note:
            sets.append("notes=COALESCE(notes,'') || ?")
            params.append(("\n" if note else "") + note)
        params.append(name)
        self.conn.execute(f"UPDATE tools SET {', '.join(sets)} WHERE name=?", params)
        self.add_event(name, "state", True, f"{state} {STATES[state]}{'：' + note if note else ''}")

    def add_event(self, name: str, kind: str, ok: bool | None, detail: str) -> None:
        self.conn.execute(
            "INSERT INTO tool_events (name, kind, ok, detail, created_at) VALUES (?,?,?,?,?)",
            (name, kind, None if ok is None else (1 if ok else 0), detail[:2000], _now()),
        )
        self.conn.commit()

    def add_evidence(self, name: str, kind: str, value: str, passed: bool) -> None:
        self.conn.execute(
            "INSERT INTO tool_evidence (name, kind, value, passed, created_at) VALUES (?,?,?,?,?)",
            (name, kind, value[:2000], 1 if passed else 0, _now()),
        )
        self.conn.commit()

    # ---------- 读取 ----------

    def get(self, name: str) -> Tool | None:
        row = self.conn.execute("SELECT * FROM tools WHERE name=?", (name,)).fetchone()
        return _row_to_tool(row) if row else None

    def list(self, *, state: str | None = None, query: str = "",
             auto_call_only: bool = False) -> list[Tool]:
        where, params = [], []
        if state:
            where.append("state=?")
            params.append(state)
        if auto_call_only:
            where.append("auto_call=1 AND state='D'")
        if query:
            where.append("(name LIKE ? OR purpose LIKE ? OR repo_url LIKE ?)")
            params.extend([f"%{query}%"] * 3)
        sql = "SELECT * FROM tools"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY CASE state WHEN 'D' THEN 0 WHEN 'C' THEN 1 WHEN 'B' THEN 2 " \
               "WHEN 'A' THEN 3 ELSE 4 END, name"
        return [_row_to_tool(row) for row in self.conn.execute(sql, params)]

    def events(self, name: str | None = None, limit: int = 50) -> list[Any]:
        if name:
            return list(self.conn.execute(
                "SELECT * FROM tool_events WHERE name=? ORDER BY id DESC LIMIT ?",
                (name, limit)))
        return list(self.conn.execute(
            "SELECT * FROM tool_events ORDER BY id DESC LIMIT ?", (limit,)))

    def evidence(self, name: str, limit: int = 50) -> list[Any]:
        return list(self.conn.execute(
            "SELECT * FROM tool_evidence WHERE name=? ORDER BY id DESC LIMIT ?", (name, limit)))

    def stats(self) -> dict[str, int]:
        counts = {state: 0 for state in STATES}
        for row in self.conn.execute("SELECT state, COUNT(*) AS n FROM tools GROUP BY state"):
            counts[row["state"]] = row["n"]
        counts["total"] = sum(counts.values())
        counts["auto_callable"] = self.conn.execute(
            "SELECT COUNT(*) FROM tools WHERE state='D' AND auto_call=1").fetchone()[0]
        return counts

    def seed_builtin(self, tools: Iterable[Tool]) -> int:
        count = 0
        for tool in tools:
            self.upsert(tool, source="内置")
            count += 1
        return count


def _row_to_tool(row: Any) -> Tool:
    data = {key: row[key] for key in row.keys()}
    for flag in ("installed", "verified", "auto_call"):
        data[flag] = bool(data.get(flag))
    return Tool(**{key: data.get(key) for key in Tool.__dataclass_fields__})


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def dump_json(tools: Iterable[Tool], path: Any) -> str:
    from pathlib import Path

    payload = [tool.as_dict() for tool in tools]
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)
