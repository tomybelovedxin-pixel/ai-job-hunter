"""SQLite 存储：岗位、话术、运行记录。只做落库与查询，不碰浏览器。"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform      TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    url           TEXT,
    title         TEXT,
    company       TEXT,
    salary_text   TEXT,
    salary_min    REAL,
    salary_max    REAL,
    city          TEXT,
    district      TEXT,
    experience    TEXT,
    education     TEXT,
    tags          TEXT,
    hr_name       TEXT,
    hr_title      TEXT,
    hr_active     TEXT,
    description   TEXT,
    raw_card      TEXT,
    status        TEXT NOT NULL DEFAULT 'new',
    score         REAL,
    score_reasons TEXT,
    gaps          TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    UNIQUE (platform, external_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs(id),
    kind       TEXT NOT NULL DEFAULT 'greeting',
    variant    INTEGER,
    text       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'draft',
    error      TEXT,
    verified   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    sent_at    TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats      TEXT
);

-- 岗位分析结果：每个岗位保留最新一份，依据和缺口都可以回读
CREATE TABLE IF NOT EXISTS analyses (
    job_id       INTEGER PRIMARY KEY REFERENCES jobs(id),
    verdict      TEXT,
    fit_score    REAL,
    requirements TEXT,
    strengths    TEXT,
    risks        TEXT,
    gaps         TEXT,
    questions    TEXT,
    engine       TEXT,
    created_at   TEXT NOT NULL
);

-- 生成的简历：direction 是目标方向，variant 区分主简历与定制版
CREATE TABLE IF NOT EXISTS resumes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER REFERENCES jobs(id),
    direction  TEXT NOT NULL,
    variant    TEXT NOT NULL DEFAULT 'tailored',
    docx_path  TEXT,
    pdf_path   TEXT,
    md_path    TEXT,
    content    TEXT,
    checks     TEXT,
    created_at TEXT NOT NULL
);

-- 求职材料：自我介绍、求职信、私信回复、面试提纲、技能缺口
CREATE TABLE IF NOT EXISTS materials (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER REFERENCES jobs(id),
    kind       TEXT NOT NULL,
    title      TEXT,
    text       TEXT NOT NULL,
    engine     TEXT,
    meta       TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS materials_job_idx ON materials(job_id, kind);

-- 应聘记录：一条岗位一条记录，状态变化进 application_events
CREATE TABLE IF NOT EXISTS applications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
    status      TEXT NOT NULL DEFAULT 'to_apply',
    resume_id   INTEGER REFERENCES resumes(id),
    applied_at  TEXT,
    channel     TEXT,
    contact     TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS application_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    status         TEXT,
    note           TEXT,
    created_at     TEXT NOT NULL
);

-- 审计流水：谁在什么时候做了什么（发送、提交、导出都记）
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    subject    TEXT,
    detail     TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);

-- 简历附件请求持久化记录：跨守护进程生命周期防重复同意
CREATE TABLE IF NOT EXISTS resume_requests (
    fingerprint TEXT PRIMARY KEY,
    company     TEXT NOT NULL,
    hr_name     TEXT,
    job         TEXT,
    snippet     TEXT,
    status      TEXT NOT NULL,
    verified    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS resume_requests_comp_idx ON resume_requests(company, hr_name);
"""

# jobs 表后续加的列：来源、有效性、去重指向、质量标记
JOB_COLUMNS = {
    "source": "TEXT",
    "fetched_at": "TEXT",
    "valid": "INTEGER",
    "validity_note": "TEXT",
    "dup_of": "INTEGER",
    "quality_flags": "TEXT",
    "first_seen_source": "TEXT",
    "description_zh": "TEXT",
    "translated_at": "TEXT",
    "title_zh": "TEXT",
}


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """老库补列：只在缺列时 ALTER，不动已有数据。"""
        have = {row["name"] for row in self.conn.execute("PRAGMA table_info(jobs)")}
        for column, kind in JOB_COLUMNS.items():
            if column not in have:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {kind}")
        if "fetched_at" not in have:
            self.conn.execute("UPDATE jobs SET fetched_at=last_seen WHERE fetched_at IS NULL")

    # ---------- 岗位 ----------

    def upsert_jobs(self, platform: str, jobs: Iterable[dict[str, Any]]) -> tuple[int, int]:
        now = _now()
        new = updated = 0
        for job in jobs:
            ext = str(job.get("external_id") or "").strip()
            if not ext:
                continue
            row = self.conn.execute(
                "SELECT id, description FROM jobs WHERE platform=? AND external_id=?",
                (platform, ext),
            ).fetchone()
            if row:
                # 只在拿到新字段时覆盖，避免把已有 JD 正文清空
                fields, values = [], []
                for key in (
                    "url", "title", "company", "salary_text", "salary_min", "salary_max",
                    "city", "district", "experience", "education", "tags", "hr_name",
                    "hr_title", "hr_active", "description", "raw_card",
                    "source", "fetched_at",
                ):
                    value = job.get(key)
                    if value in (None, "", []):
                        continue
                    fields.append(f"{key}=?")
                    values.append(_jsonify(_decode(value)))
                fields.append("last_seen=?")
                values.append(now)
                values.append(row["id"])
                self.conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id=?", values)
                updated += 1
            else:
                self.conn.execute(
                    """INSERT INTO jobs
                       (platform, external_id, url, title, company, salary_text, salary_min,
                        salary_max, city, district, experience, education, tags, hr_name,
                        hr_title, hr_active, description, raw_card, first_seen, last_seen,
                        source, fetched_at, first_seen_source)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        platform, ext, job.get("url"), _decode(job.get("title")),
                        _decode(job.get("company")),
                        _decode(job.get("salary_text")), job.get("salary_min"), job.get("salary_max"),
                        _decode(job.get("city")), _decode(job.get("district")),
                        _decode(job.get("experience")),
                        _decode(job.get("education")), _jsonify(job.get("tags")), job.get("hr_name"),
                        job.get("hr_title"), job.get("hr_active"), job.get("description"),
                        _jsonify(job.get("raw_card")), now, now,
                        job.get("source") or platform, job.get("fetched_at") or now,
                        job.get("source") or platform,
                    ),
                )
                new += 1
        self.conn.commit()
        return new, updated

    def list_jobs(
        self,
        *,
        status: str | None = None,
        statuses: tuple[str, ...] | None = None,
        min_score: float | None = None,
        limit: int | None = None,
        order: str = "score",
    ) -> list[sqlite3.Row]:
        where, params = [], []
        if status:
            where.append("status=?")
            params.append(status)
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            params.extend(statuses)
        if min_score is not None:
            where.append("score IS NOT NULL AND score>=?")
            params.append(min_score)
        order_by = {
            "score": "score IS NULL, score DESC, id DESC",
            "recent": "first_seen DESC",
            "id": "id",
        }.get(order, "id")
        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order_by}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return list(self.conn.execute(sql, params))

    def get_job(self, job_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def save_score(self, job_id: int, score: float, reasons: list[str], gaps: list[str]) -> None:
        self.conn.execute(
            "UPDATE jobs SET score=?, score_reasons=?, gaps=?, status="
            "CASE WHEN status='new' THEN 'scored' ELSE status END WHERE id=?",
            (score, _jsonify(reasons), _jsonify(gaps), job_id),
        )
        self.conn.commit()

    def set_status(self, job_id: int, status: str) -> None:
        self.conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
        self.conn.commit()

    def set_description(self, job_id: int, description: str) -> None:
        self.conn.execute("UPDATE jobs SET description=? WHERE id=?", (description, job_id))
        self.conn.commit()

    # ---------- 话术 ----------

    def add_message(self, job_id: int, text: str, variant: int | None = None,
                    kind: str = "greeting") -> int:
        cur = self.conn.execute(
            "INSERT INTO messages (job_id, kind, variant, text, created_at) VALUES (?,?,?,?,?)",
            (job_id, kind, variant, text, _now()),
        )
        self.conn.execute(
            "UPDATE jobs SET status='drafted' WHERE id=? AND status IN ('new','scored')",
            (job_id,),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def draft_for_job(self, job_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM messages WHERE job_id=? AND kind='greeting' "
            "ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()

    def mark_message(self, message_id: int, status: str, *, error: str | None = None,
                     verified: bool = False) -> None:
        self.conn.execute(
            "UPDATE messages SET status=?, error=?, verified=?, sent_at=? WHERE id=?",
            (status, error, 1 if verified else 0, _now() if status == "sent" else None, message_id),
        )
        self.conn.commit()

    def queued_messages(self, limit: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT m.*, j.external_id, j.title, j.company, j.url, j.score "
                "FROM messages m "
                "JOIN jobs j ON j.id = m.job_id "
                "WHERE m.status='draft' ORDER BY j.score DESC LIMIT ?",
                (limit,),
            )
        )

    # ---------- 运行记录 ----------

    def start_run(self, kind: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (kind, started_at) VALUES (?,?)", (kind, _now())
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, stats: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, stats=? WHERE id=?",
            (_now(), _jsonify(stats), run_id),
        )
        self.conn.commit()

    def stats(self) -> dict[str, Any]:
        jobs = self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        by_status = {
            row["status"]: row["n"]
            for row in self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            )
        }
        msgs = {
            row["status"]: row["n"]
            for row in self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM messages GROUP BY status"
            )
        }
        apps = {
            row["status"]: row["n"]
            for row in self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM applications GROUP BY status"
            )
        }
        return {
            "jobs": jobs,
            "jobs_by_status": by_status,
            "messages": msgs,
            "applications": apps,
            "analyses": self.conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0],
            "resumes": self.conn.execute("SELECT COUNT(*) FROM resumes").fetchone()[0],
            "materials": self.conn.execute("SELECT COUNT(*) FROM materials").fetchone()[0],
        }

    # ---------- 岗位分析与有效性 ----------

    def save_analysis(self, job_id: int, data: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO analyses
               (job_id, verdict, fit_score, requirements, strengths, risks, gaps,
                questions, engine, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET
                 verdict=excluded.verdict, fit_score=excluded.fit_score,
                 requirements=excluded.requirements, strengths=excluded.strengths,
                 risks=excluded.risks, gaps=excluded.gaps,
                 questions=excluded.questions, engine=excluded.engine,
                 created_at=excluded.created_at""",
            (
                job_id,
                data.get("verdict"),
                data.get("fit_score"),
                _jsonify(data.get("requirements") or []),
                _jsonify(data.get("strengths") or []),
                _jsonify(data.get("risks") or []),
                _jsonify(data.get("gaps") or []),
                _jsonify(data.get("questions") or []),
                data.get("engine"),
                _now(),
            ),
        )
        self.conn.execute(
            "UPDATE jobs SET status='analyzed' WHERE id=? AND status IN ('new','scored','drafted')",
            (job_id,),
        )
        self.conn.commit()

    def get_analysis(self, job_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM analyses WHERE job_id=?", (job_id,)).fetchone()

    def mark_validity(self, job_id: int, valid: bool | None, note: str,
                      flags: list[str] | None = None) -> None:
        self.conn.execute(
            "UPDATE jobs SET valid=?, validity_note=?, quality_flags=? WHERE id=?",
            (None if valid is None else int(bool(valid)), note,
             _jsonify(flags or []), job_id),
        )
        self.conn.commit()

    def mark_duplicate(self, job_id: int, original_id: int) -> None:
        self.conn.execute("UPDATE jobs SET dup_of=? WHERE id=?", (original_id, job_id))
        self.conn.commit()

    # ---------- 简历 ----------

    def add_resume(self, *, direction: str, variant: str, job_id: int | None,
                   docx_path: str | None, pdf_path: str | None, md_path: str | None,
                   content: dict[str, Any], checks: dict[str, Any]) -> int:
        cursor = self.conn.execute(
            """INSERT INTO resumes
               (job_id, direction, variant, docx_path, pdf_path, md_path, content, checks, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (job_id, direction, variant, docx_path, pdf_path, md_path,
             _jsonify(content), _jsonify(checks), _now()),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def get_resume(self, resume_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM resumes WHERE id=?", (resume_id,)).fetchone()

    def list_resumes(self, *, job_id: int | None = None, limit: int = 50) -> list[sqlite3.Row]:
        sql = "SELECT * FROM resumes"
        params: list[Any] = []
        if job_id is not None:
            sql += " WHERE job_id=?"
            params.append(job_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return list(self.conn.execute(sql, params))

    # ---------- 求职材料 ----------

    def add_material(self, *, job_id: int | None, kind: str, title: str, text: str,
                     engine: str = "rule", meta: dict[str, Any] | None = None) -> int:
        cursor = self.conn.execute(
            """INSERT INTO materials (job_id, kind, title, text, engine, meta, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (job_id, kind, title, text, engine, _jsonify(meta or {}), _now()),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def list_materials(self, *, job_id: int | None = None, kind: str | None = None,
                       limit: int = 100) -> list[sqlite3.Row]:
        where, params = [], []
        if job_id is not None:
            where.append("job_id=?")
            params.append(job_id)
        if kind:
            where.append("kind=?")
            params.append(kind)
        sql = "SELECT * FROM materials"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return list(self.conn.execute(sql, params))

    # ---------- 应聘记录 ----------

    APPLICATION_STATUSES = (
        "to_apply", "applied", "chatting", "interview", "offer", "rejected", "dropped",
    )

    def ensure_application(self, job_id: int, *, status: str = "to_apply",
                           note: str | None = None) -> int:
        row = self.conn.execute("SELECT id FROM applications WHERE job_id=?", (job_id,)).fetchone()
        if row:
            return int(row["id"])
        now = _now()
        cursor = self.conn.execute(
            """INSERT INTO applications (job_id, status, note, created_at, updated_at)
               VALUES (?,?,?,?,?)""",
            (job_id, status, note, now, now),
        )
        app_id = int(cursor.lastrowid)
        self.conn.execute(
            "INSERT INTO application_events (application_id, status, note, created_at) VALUES (?,?,?,?)",
            (app_id, status, note or "建档", now),
        )
        self.conn.commit()
        return app_id

    def set_application_status(
        self,
        job_id: int,
        status: str,
        *,
        note: str | None = None,
        resume_id: int | None = None,
        channel: str | None = None,
        contact: str | None = None,
    ) -> int:
        if status not in self.APPLICATION_STATUSES:
            raise ValueError(f"未知状态: {status}")
        app_id = self.ensure_application(job_id)
        now = _now()
        applied_at = now if status in ("applied", "chatting", "interview", "offer") else None
        fields = ["status=?", "updated_at=?", "note=COALESCE(?, note)"]
        values: list[Any] = [status, now, note]
        if resume_id:
            fields.append("resume_id=?")
            values.append(resume_id)
        if channel:
            fields.append("channel=?")
            values.append(channel)
        if contact:
            fields.append("contact=?")
            values.append(contact)
        if applied_at:
            row = self.conn.execute(
                "SELECT applied_at FROM applications WHERE id=?", (app_id,)
            ).fetchone()
            if not row or not row["applied_at"]:
                fields.append("applied_at=?")
                values.append(applied_at)
        values.append(app_id)
        self.conn.execute(f"UPDATE applications SET {', '.join(fields)} WHERE id=?", values)
        self.conn.execute(
            "INSERT INTO application_events (application_id, status, note, created_at) VALUES (?,?,?,?)",
            (app_id, status, note, now),
        )
        self.conn.commit()
        return app_id

    def list_applications(self, *, statuses: tuple[str, ...] | None = None,
                          limit: int = 200) -> list[sqlite3.Row]:
        sql = (
            "SELECT a.*, j.title, j.company, j.city, j.url, j.platform, j.score, "
            "j.validity_note, r.docx_path AS resume_docx, r.pdf_path AS resume_pdf, "
            "r.direction AS resume_direction "
            "FROM applications a JOIN jobs j ON j.id=a.job_id "
            "LEFT JOIN resumes r ON r.id=a.resume_id"
        )
        params: list[Any] = []
        if statuses:
            sql += f" WHERE a.status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        sql += " ORDER BY a.updated_at DESC LIMIT ?"
        params.append(limit)
        return list(self.conn.execute(sql, params))

    def application_events(self, application_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM application_events WHERE application_id=? ORDER BY id",
                (application_id,),
            )
        )

    # ---------- 审计与设置 ----------

    # 这些状态代表"我们已经对这家公司做过对外动作"（草稿不算）
    CONTACTED_STATUSES = ("sent", "unverified", "failed")

    def contacted_companies(self) -> dict[str, dict[str, Any]]:
        """公司 → 联系历史汇总。同一家公司的多个岗位合并成一条。

        用途是去重：生成新话术前先看这里，已经联系过的公司不再重复打扰。
        基准有两个，都来自数据库、不依赖手写清单：
        - messages.status ∈ {sent, unverified, failed}（有真实话术行）；
        - jobs.status ∈ {sent, sent_unverified, send_failed}（2026-10-15 发现的历史遗留：
          有岗位被标成 sent 却没有对应的消息行，只按消息表去重会漏掉这些公司）。
        """
        rows = self.conn.execute(
            "SELECT j.id AS job_id, j.company AS company, j.title AS title, "
            "j.status AS job_status, j.score AS score, j.city AS city, "
            "m.status AS msg_status, m.sent_at AS sent_at, m.created_at AS created_at, "
            "COALESCE(m.created_at, j.last_seen) AS touched_at "
            "FROM jobs j LEFT JOIN messages m "
            "  ON m.job_id = j.id AND m.status IN ('sent','unverified','failed') "
            "WHERE j.company IS NOT NULL AND j.company <> '' AND ("
            "  m.id IS NOT NULL OR j.status IN ('sent','sent_unverified','send_failed')) "
            "ORDER BY j.id"
        ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = out.setdefault(row["company"], {
                "company": row["company"], "jobs": [], "statuses": set(),
                "last_contact_at": "", "last_sent_at": "",
            })
            item["jobs"].append({
                "job_id": row["job_id"], "title": row["title"], "city": row["city"],
                "score": row["score"], "job_status": row["job_status"],
                "msg_status": row["msg_status"] or "job_status_only",
            })
            item["statuses"].add(row["msg_status"] or "job_status_only")
            stamp = row["sent_at"] or row["touched_at"] or ""
            if stamp > item["last_contact_at"]:
                item["last_contact_at"] = stamp
            if row["msg_status"] == "sent" and stamp > item["last_sent_at"]:
                item["last_sent_at"] = stamp
        for item in out.values():
            item["statuses"] = sorted(item["statuses"])
        return out

    def contacted_company_names(self) -> set[str]:
        return set(self.contacted_companies())

    def company_history(self, keyword: str) -> list[dict[str, Any]]:
        """按公司名关键字查联系历史（岗位、分析、话术、应聘记录）。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return []
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE company LIKE ? OR title LIKE ? "
            "ORDER BY id DESC LIMIT 20",
            (f"%{keyword}%", f"%{keyword}%"),
        ).fetchall()
        history = []
        for job in rows:
            analysis = self.get_analysis(job["id"])
            messages = self.conn.execute(
                "SELECT status, verified, sent_at, created_at, substr(text,1,120) AS text "
                "FROM messages WHERE job_id=? ORDER BY id DESC LIMIT 3",
                (job["id"],),
            ).fetchall()
            application = self.conn.execute(
                "SELECT status, updated_at, note FROM applications WHERE job_id=?",
                (job["id"],),
            ).fetchone()
            history.append({
                "job": {
                    "id": job["id"], "title": job["title"], "company": job["company"],
                    "city": job["city"], "salary_text": job["salary_text"],
                    "url": job["url"], "status": job["status"], "score": job["score"],
                    "platform": job["platform"], "external_id": job["external_id"],
                    "last_seen": job["last_seen"],
                },
                "analysis": None if not analysis else {
                    "verdict": analysis["verdict"], "fit_score": analysis["fit_score"],
                    "created_at": analysis["created_at"], "engine": analysis["engine"],
                    "gaps": loads(analysis["gaps"], []),
                },
                "messages": [dict(row) for row in messages],
                "application": None if not application else {
                    "status": application["status"], "updated_at": application["updated_at"],
                    "note": application["note"],
                },
            })
        return history

    def analysis_reusable(self, job_id: int, *, hours: float) -> tuple[bool, str]:
        """能不能直接复用已有分析（"信息没变就不重复分析"）。

        两个条件同时满足才算可复用：分析结果在有效期内，且岗位数据自那之后没被重新抓过
        （jobs.fetched_at 晚于分析时间 = JD 有新内容，必须重算）。
        """
        row = self.conn.execute(
            "SELECT a.created_at AS analyzed_at, j.fetched_at AS fetched_at, j.last_seen AS last_seen "
            "FROM analyses a JOIN jobs j ON j.id = a.job_id WHERE a.job_id=?",
            (job_id,),
        ).fetchone()
        if not row or not row["analyzed_at"]:
            return False, "没有历史分析"
        if hours <= 0:
            return False, "未开启复用"
        try:
            created = time.mktime(time.strptime(row["analyzed_at"][:19], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return False, "分析时间无法解析"
        age_hours = (time.time() - created) / 3600.0
        if age_hours >= hours:
            return False, f"分析已过有效期（{age_hours:.1f}h）"
        fetched = row["fetched_at"] or row["last_seen"] or ""
        if fetched and fetched > row["analyzed_at"]:
            return False, "岗位信息在那之后更新过"
        return True, f"复用 {age_hours:.1f}h 前的分析"

    def log_event(self, kind: str, subject: str = "", detail: str = "") -> int:
        cursor = self.conn.execute(
            "INSERT INTO events (kind, subject, detail, created_at) VALUES (?,?,?,?)",
            (kind, subject, detail, _now()),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def recent_events(self, limit: int = 50, kind: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM events"
        params: list[Any] = []
        if kind:
            sql += " WHERE kind=?"
            params.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return list(self.conn.execute(sql, params))

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return default if not row else loads(row["value"], default)

    def set_setting(self, key: str, value: Any) -> None:
        self.conn.execute(
            """INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                              updated_at=excluded.updated_at""",
            (key, json.dumps(value, ensure_ascii=False), _now()),
        )
        self.conn.commit()

    def record_resume_request(
        self,
        fingerprint: str,
        company: str,
        hr_name: str = "",
        job: str = "",
        snippet: str = "",
        status: str = "pending",
        verified: bool = False,
        note: str = "",
    ) -> None:
        """记录或更新简历请求处理状态（持久化防重）。"""
        now = _now()
        self.conn.execute(
            """INSERT INTO resume_requests (
                   fingerprint, company, hr_name, job, snippet, status, verified, created_at, updated_at, note
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(fingerprint) DO UPDATE SET
                   status=excluded.status,
                   verified=excluded.verified,
                   updated_at=excluded.updated_at,
                   note=excluded.note""",
            (fingerprint, company, hr_name, job, snippet, status, int(verified), now, now, note),
        )
        self.conn.commit()

    def get_resume_request(self, fingerprint: str) -> dict[str, Any] | None:
        """根据指纹查询简历请求记录。"""
        row = self.conn.execute(
            "SELECT fingerprint, company, hr_name, job, snippet, status, verified, created_at, updated_at, note FROM resume_requests WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if not row:
            return None
        return {
            "fingerprint": row["fingerprint"],
            "company": row["company"],
            "hr_name": row["hr_name"],
            "job": row["job"],
            "snippet": row["snippet"],
            "status": row["status"],
            "verified": bool(row["verified"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "note": row["note"],
        }

    def close(self) -> None:
        self.conn.close()

    # 支持 with 写法，测试和脚本里不容易忘记关库
    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _jsonify(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def _decode(value: Any) -> Any:
    """入库前把图标字体留下的私有区数字还原成普通数字（BOSS 薪资那类）。"""
    if not isinstance(value, str):
        return value
    from .textutil import has_pua_digits, decode_pua_digits

    return decode_pua_digits(value) if has_pua_digits(value) else value


def loads(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
