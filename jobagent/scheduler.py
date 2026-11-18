"""定时检索：可开可关、有日志、有停止方式，状态如实反映。

不用常驻服务包装，就是一个独立的 Python 进程：命令行启动时后台跑循环，
状态靠 PID 文件和心跳时间判断，随时可以停。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import ROOT, load as load_config
from .store import Store

STATE_DIR = ROOT / "data" / "scheduler"
PID_FILE = STATE_DIR / "pid.txt"
LOG_FILE = STATE_DIR / "scheduler.log"
CONF_FILE = STATE_DIR / "config.json"

DEFAULT_CONF = {
    "interval_minutes": 180,
    "keywords": [],
    "cities": [],
    "per_source_limit": 20,
    "sources": ["tencent", "remoteok", "jobicy", "remotive", "hn"],
    "auto_analyze": True,
}


def load_conf() -> dict[str, Any]:
    conf = dict(DEFAULT_CONF)
    if CONF_FILE.exists():
        try:
            conf.update(json.loads(CONF_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    if not conf.get("keywords") or not conf.get("cities"):
        config = load_config()
        conf["keywords"] = conf.get("keywords") or config.search.keywords
        conf["cities"] = conf.get("cities") or config.search.cities
    return conf


def save_conf(conf: dict[str, Any]) -> str:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CONF_FILE.write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(CONF_FILE)


def log(message: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{stamp}] {message}\n")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=15)
        return str(pid) in (out.stdout or "")
    except Exception:
        try:
            import os

            os.kill(pid, 0)
            return True
        except Exception:
            return False


def read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip() or 0) or None
    except Exception:
        return None


def start(*, interval_minutes: int | None = None, keywords: list[str] | None = None,
          cities: list[str] | None = None) -> dict[str, Any]:
    conf = load_conf()
    if interval_minutes:
        conf["interval_minutes"] = int(interval_minutes)
    if keywords:
        conf["keywords"] = keywords
    if cities:
        conf["cities"] = cities
    save_conf(conf)
    pid = read_pid()
    if pid and _pid_alive(pid):
        return {"started": False, "reason": f"已经在跑（PID {pid}）", "pid": pid,
                "interval_minutes": conf["interval_minutes"]}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    creation = 0
    if sys.platform == "win32":
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [sys.executable, "-m", "jobagent", "schedule", "run"],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=creation,
    )
    PID_FILE.write_text(str(process.pid), encoding="utf-8")
    log(f"启动定时检索，PID {process.pid}，间隔 {conf['interval_minutes']} 分钟")
    return {"started": True, "pid": process.pid,
            "interval_minutes": conf["interval_minutes"], "log": str(LOG_FILE)}


def stop() -> dict[str, Any]:
    pid = read_pid()
    if not pid:
        return {"stopped": False, "reason": "没有记录到运行中的进程"}
    if not _pid_alive(pid):
        PID_FILE.unlink(missing_ok=True)
        return {"stopped": False, "reason": f"PID {pid} 已经不在了，清理记录"}
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True, timeout=30)
    except Exception as exc:
        return {"stopped": False, "reason": f"停止失败：{type(exc).__name__}: {exc}"}
    PID_FILE.unlink(missing_ok=True)
    log(f"已停止定时检索（PID {pid}）")
    return {"stopped": True, "pid": pid}


def status() -> dict[str, Any]:
    pid = read_pid()
    conf = load_conf()
    alive = bool(pid and _pid_alive(pid))
    heartbeat = STATE_DIR / "heartbeat.txt"
    last = heartbeat.read_text(encoding="utf-8").strip() if heartbeat.exists() else ""
    return {
        "running": alive,
        "pid": pid if alive else None,
        "interval_minutes": conf.get("interval_minutes"),
        "last_run": last,
        "keywords": conf.get("keywords"),
        "cities": conf.get("cities"),
        "log": str(LOG_FILE),
        "config": str(CONF_FILE),
    }


def logs(lines: int = 40) -> list[str]:
    if not LOG_FILE.exists():
        return []
    content = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    return content[-lines:]


def run_forever() -> int:
    """后台循环本体。前台也能直接跑，Ctrl+C 即停。"""
    conf = load_conf()
    interval = max(5, int(conf.get("interval_minutes", 180))) * 60
    log("循环开始")
    while True:
        try:
            summary = run_once(conf)
            log(f"本轮完成：{json.dumps(summary, ensure_ascii=False)}")
        except Exception as exc:
            log(f"本轮失败：{type(exc).__name__}: {exc}")
        (STATE_DIR / "heartbeat.txt").write_text(
            time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        log(f"下一轮在 {interval // 60} 分钟后")
        time.sleep(interval)


def run_once(conf: dict[str, Any] | None = None) -> dict[str, Any]:
    """跑一轮：取岗位 → 分析新岗位 → 落库。只读网络，不发送任何消息。"""
    from .analyze import analyze_text
    from .collect import collect
    from .sources import Filters, get as get_source

    conf = conf or load_conf()
    config = load_config()
    store = Store(config.db_path)
    filters = Filters(
        keywords=list(conf.get("keywords") or []),
        cities=list(conf.get("cities") or []),
        limit=int(conf.get("per_source_limit", 20)),
        pages=1,
    )
    sources = [get_source(name) for name in (conf.get("sources") or [])]
    report = collect(store, sources, filters, pause=0.5)
    analyzed = 0
    if conf.get("auto_analyze", True):
        for job in store.list_jobs(status="new", limit=40):
            analysis = analyze_text(
                job["description"] or "", title=job["title"] or "", company=job["company"] or "",
                city=job["city"] or "", salary_text=job["salary_text"] or "",
                experience=job["experience"] or "", education=job["education"] or "",
            )
            store.save_analysis(job["id"], analysis)
            store.save_score(job["id"], analysis["fit_score"],
                             analysis["strengths"], analysis["gaps"])
            analyzed += 1
    store.log_event("schedule_run", "定时检索",
                    f"新增 {report.totals['new']} 条，分析 {analyzed} 条")
    store.close()
    return {"new": report.totals["new"], "analyzed": analyzed,
            "failed_sources": report.totals["failed_sources"]}


def control(action: str, **kwargs: Any) -> dict[str, Any]:
    action = (action or "status").lower()
    if action == "start":
        return start(**kwargs)
    if action == "stop":
        return stop()
    if action == "logs":
        return {"lines": logs(int(kwargs.get("lines", 40)))}
    if action == "run":
        return run_once()
    return status()
