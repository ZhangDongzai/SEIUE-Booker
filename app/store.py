"""SQLite 访问层。单文件数据库，WAL 模式，每次操作独立连接。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from . import config, crypto

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('list', 'order')),
    method     TEXT NOT NULL DEFAULT 'GET',
    url        TEXT NOT NULL DEFAULT '',
    headers    TEXT NOT NULL DEFAULT '{}',
    body       TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tasks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    enabled           INTEGER NOT NULL DEFAULT 1,
    schedule_mode     TEXT NOT NULL DEFAULT 'daily',
    open_time         TEXT NOT NULL DEFAULT '00:00:00',
    once_at           TEXT NOT NULL DEFAULT '',
    target_date       TEXT NOT NULL DEFAULT '',
    day_offset        INTEGER NOT NULL DEFAULT 7,
    venues            TEXT NOT NULL DEFAULT '',
    venue_ids         TEXT NOT NULL DEFAULT '',
    slot_start        TEXT NOT NULL DEFAULT '19:00',
    slot_end          TEXT NOT NULL DEFAULT '20:00',
    split_minutes     INTEGER NOT NULL DEFAULT 0,
    order_type        TEXT NOT NULL DEFAULT 'single_day',
    capacity          INTEGER NOT NULL DEFAULT 2,
    description       TEXT NOT NULL DEFAULT '',
    list_template_id  INTEGER,
    order_template_id INTEGER,
    map_items         TEXT NOT NULL DEFAULT '',
    map_venue_name    TEXT NOT NULL DEFAULT '',
    map_venue_id      TEXT NOT NULL DEFAULT '',
    map_venue_num     TEXT NOT NULL DEFAULT '',
    map_slot_start    TEXT NOT NULL DEFAULT '',
    map_slot_end      TEXT NOT NULL DEFAULT '',
    map_slot_id       TEXT NOT NULL DEFAULT '',
    map_available     TEXT NOT NULL DEFAULT '',
    map_available_true TEXT NOT NULL DEFAULT '',
    extras            TEXT NOT NULL DEFAULT '[]',
    success_mode      TEXT NOT NULL DEFAULT 'http2xx',
    success_expr      TEXT NOT NULL DEFAULT '',
    max_attempts      INTEGER NOT NULL DEFAULT 5,
    interval_ms       INTEGER NOT NULL DEFAULT 300,
    prewarm_ms        INTEGER NOT NULL DEFAULT 8000,
    dry_run           INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT '',
    updated_at        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER,
    trigger     TEXT NOT NULL DEFAULT 'schedule',
    planned_at  TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'running',
    message     TEXT NOT NULL DEFAULT '',
    resolved    TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS attempts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL,
    seq              INTEGER NOT NULL DEFAULT 0,
    venue_name       TEXT NOT NULL DEFAULT '',
    method           TEXT NOT NULL DEFAULT '',
    url              TEXT NOT NULL DEFAULT '',
    request_headers  TEXT NOT NULL DEFAULT '',
    request_body     TEXT NOT NULL DEFAULT '',
    http_status      INTEGER NOT NULL DEFAULT 0,
    duration_ms      REAL NOT NULL DEFAULT 0,
    ok               INTEGER NOT NULL DEFAULT 0,
    response_excerpt TEXT NOT NULL DEFAULT '',
    error            TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_attempts_run ON attempts(run_id, id);
"""

TASK_COLUMNS = [
    "name", "enabled", "schedule_mode", "open_time", "once_at", "target_date", "day_offset",
    "venues", "venue_ids", "slot_start", "slot_end", "split_minutes",
    "order_type", "capacity", "description",
    "list_template_id", "order_template_id",
    "map_items", "map_venue_name", "map_venue_id", "map_venue_num",
    "map_slot_start", "map_slot_end", "map_slot_id",
    "map_available", "map_available_true", "extras",
    "success_mode", "success_expr", "max_attempts", "interval_ms",
    "prewarm_ms", "dry_run",
]

# 旧库升级：列名 -> 建表语句片段（只增不减，保证数据不丢）
MIGRATIONS = {
    "target_date": "TEXT NOT NULL DEFAULT ''",
    "venue_ids": "TEXT NOT NULL DEFAULT ''",
    "split_minutes": "INTEGER NOT NULL DEFAULT 0",
    "order_type": "TEXT NOT NULL DEFAULT 'single_day'",
    "capacity": "INTEGER NOT NULL DEFAULT 2",
    "description": "TEXT NOT NULL DEFAULT ''",
}


def now_iso() -> str:
    return datetime.now(config.TZ).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def _db():
    """每次用完显式关闭：长期运行时不依赖 GC 回收文件句柄。"""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    config.ensure_dirs()
    with _db() as conn:
        conn.executescript(SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        for column, decl in MIGRATIONS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {decl}")
        conn.commit()

        # 老任务回填 target_date：按原来的 day_offset 换算一次，之后就以固定日期为准
        rows = conn.execute(
            "SELECT id, day_offset FROM tasks WHERE target_date IS NULL OR target_date = ''"
        ).fetchall()
        if rows:
            today = datetime.now(config.TZ).date()
            for row in rows:
                try:
                    offset = int(row["day_offset"] or 0)
                except (TypeError, ValueError):
                    offset = 0
                conn.execute("UPDATE tasks SET target_date = ? WHERE id = ?",
                             ((today + timedelta(days=offset)).isoformat(), row["id"]))
            conn.commit()


def query(sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with _db() as conn:
        return list(conn.execute(sql, tuple(args)).fetchall())


def query_one(sql: str, args: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: Iterable[Any] = ()) -> int:
    with _db() as conn:
        cur = conn.execute(sql, tuple(args))
        conn.commit()
        return cur.lastrowid or 0


# ---------------------------------------------------------------- settings

def get_setting(key: str, default: Any = None) -> Any:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: Any) -> None:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def session_headers() -> dict[str, str]:
    return crypto.decrypt_json(get_setting("session_headers", ""), default={}) or {}


def set_session_headers(headers: dict[str, str]) -> None:
    set_setting("session_headers", crypto.encrypt_json(headers))


# ---------------------------------------------------------------- 令牌自动续期

def get_refresh_token() -> str:
    """refresh_token 是服务端加密的不透明长串，同样加密落盘。"""
    return crypto.decrypt_text(str(get_setting("refresh_token", "") or ""))


def set_refresh_token(token: str) -> None:
    token = (token or "").strip()
    if token:
        set_setting("refresh_token", crypto.encrypt_text(token))
    else:
        execute("DELETE FROM settings WHERE key = 'refresh_token'")


# ---------------------------------------------------------------- 免密换令牌（passport 会话）

def get_passport_cookie() -> str:
    """passport 域的会话 cookie（HttpOnly，需从浏览器 F12 面板复制）。"""
    return crypto.decrypt_text(str(get_setting("passport_cookie", "") or ""))


def set_passport_cookie(cookie: str) -> None:
    cookie = (cookie or "").strip()
    if cookie:
        set_setting("passport_cookie", crypto.encrypt_text(cookie))
    else:
        execute("DELETE FROM settings WHERE key = 'passport_cookie'")


def token_refresh_state() -> dict:
    raw = get_setting("token_refresh_state", "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def set_token_refresh_state(payload: dict) -> None:
    set_setting("token_refresh_state", json.dumps(payload, ensure_ascii=False))


def last_check() -> dict:
    raw = get_setting("last_check", "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def set_last_check(payload: dict) -> None:
    set_setting("last_check", json.dumps(payload, ensure_ascii=False))


# ---------------------------------------------------------------- 场地清单缓存

def get_venues() -> list[dict]:
    raw = get_setting("venues_cache", "")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def set_venues(rows: list[dict], at: str = "") -> None:
    set_setting("venues_cache", json.dumps(rows, ensure_ascii=False))
    set_setting("venues_cache_at", at or now_iso())


def venues_synced_at() -> str:
    return str(get_setting("venues_cache_at", "") or "")


# ---------------------------------------------------------------- templates

def list_templates(kind: Optional[str] = None) -> list[sqlite3.Row]:
    if kind:
        return query("SELECT * FROM templates WHERE kind = ? ORDER BY id DESC", (kind,))
    return query("SELECT * FROM templates ORDER BY kind, id DESC")


def get_template(tpl_id: Optional[int]) -> Optional[sqlite3.Row]:
    if not tpl_id:
        return None
    return query_one("SELECT * FROM templates WHERE id = ?", (tpl_id,))


def save_template(data: dict, tpl_id: Optional[int] = None) -> int:
    ts = now_iso()
    fields = ("name", "kind", "method", "url", "headers", "body", "note")
    values = [data.get(k, "") for k in fields]
    if tpl_id:
        sets = ", ".join(f"{k} = ?" for k in fields)
        execute(
            f"UPDATE templates SET {sets}, updated_at = ? WHERE id = ?",
            (*values, ts, tpl_id),
        )
        return tpl_id
    placeholders = ", ".join("?" for _ in fields)
    return execute(
        f"INSERT INTO templates({', '.join(fields)}, created_at, updated_at) "
        f"VALUES({placeholders}, ?, ?)",
        (*values, ts, ts),
    )


def delete_template(tpl_id: int) -> None:
    execute("UPDATE tasks SET list_template_id = NULL WHERE list_template_id = ?", (tpl_id,))
    execute("UPDATE tasks SET order_template_id = NULL WHERE order_template_id = ?", (tpl_id,))
    execute("DELETE FROM templates WHERE id = ?", (tpl_id,))


# ---------------------------------------------------------------- tasks

def list_tasks() -> list[sqlite3.Row]:
    return query("SELECT * FROM tasks ORDER BY id DESC")


def get_task(task_id: int) -> Optional[sqlite3.Row]:
    return query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))


def task_form_defaults() -> dict:
    today = datetime.now(config.TZ).date()
    return {
        "id": None,
        "name": "",
        "enabled": 1,
        "schedule_mode": "daily",
        "open_time": "00:00:30",
        "once_at": "",
        "target_date": (today + timedelta(days=config.MAX_ORDER_DAYS)).isoformat(),
        "day_offset": config.MAX_ORDER_DAYS,
        "venues": "",
        "venue_ids": "",
        "slot_start": "19:00",
        "slot_end": "20:00",
        "split_minutes": 0,
        "order_type": "single_day",
        "capacity": 2,
        "description": "打球",
        "list_template_id": "",
        "order_template_id": "",
        "map_items": "",
        "map_venue_name": "name",
        "map_venue_id": "id",
        "map_venue_num": "",
        "map_slot_start": "",
        "map_slot_end": "",
        "map_slot_id": "",
        "map_available": "",
        "map_available_true": "",
        "extras": "[]",
        "success_mode": "http2xx",
        "success_expr": "",
        "max_attempts": 5,
        "interval_ms": 300,
        "prewarm_ms": 8000,
        "dry_run": 0,
    }


def save_task(data: dict, task_id: Optional[int] = None) -> int:
    ts = now_iso()
    values = [data.get(k, "") for k in TASK_COLUMNS]
    if task_id:
        sets = ", ".join(f"{k} = ?" for k in TASK_COLUMNS)
        execute(f"UPDATE tasks SET {sets}, updated_at = ? WHERE id = ?", (*values, ts, task_id))
        return task_id
    placeholders = ", ".join("?" for _ in TASK_COLUMNS)
    return execute(
        f"INSERT INTO tasks({', '.join(TASK_COLUMNS)}, created_at, updated_at) "
        f"VALUES({placeholders}, ?, ?)",
        (*values, ts, ts),
    )


def set_task_enabled(task_id: int, enabled: bool) -> None:
    execute("UPDATE tasks SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, now_iso(), int(task_id)))


def delete_task(task_id: int) -> None:
    execute("DELETE FROM attempts WHERE run_id IN (SELECT id FROM runs WHERE task_id = ?)", (task_id,))
    execute("DELETE FROM runs WHERE task_id = ?", (task_id,))
    execute("DELETE FROM tasks WHERE id = ?", (task_id,))


# ---------------------------------------------------------------- runs

def create_run(task_row, trigger: str, planned_at: str, started_at: str) -> int:
    return execute(
        "INSERT INTO runs(task_id, trigger, planned_at, started_at, status, message, resolved) "
        "VALUES(?, ?, ?, ?, 'running', '', '{}')",
        (task_row["id"], trigger, planned_at, started_at),
    )


def finish_run(run_id: int, status: str, message: str, resolved: dict) -> None:
    execute(
        "UPDATE runs SET status = ?, message = ?, resolved = ?, finished_at = ? WHERE id = ?",
        (status, message[:2000], json.dumps(resolved, ensure_ascii=False)[:20000], now_iso(), run_id),
    )


def add_attempt(run_id: int, seq: int, *, venue_name: str, method: str, url: str,
                request_headers: str, request_body: str, http_status: int,
                duration_ms: float, ok: bool, response_excerpt: str, error: str) -> int:
    return execute(
        "INSERT INTO attempts(run_id, seq, venue_name, method, url, request_headers, request_body, "
        "http_status, duration_ms, ok, response_excerpt, error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, seq, venue_name, method, url, request_headers[:8000], request_body[:8000],
         http_status, duration_ms, 1 if ok else 0, response_excerpt[:8000], error[:2000]),
    )


def list_runs(limit: int = 80) -> list[sqlite3.Row]:
    return query(
        "SELECT r.*, t.name AS task_name FROM runs r LEFT JOIN tasks t ON t.id = r.task_id "
        "ORDER BY r.id DESC LIMIT ?",
        (limit,),
    )


def get_run(run_id: int) -> Optional[sqlite3.Row]:
    return query_one(
        "SELECT r.*, t.name AS task_name FROM runs r LEFT JOIN tasks t ON t.id = r.task_id WHERE r.id = ?",
        (run_id,),
    )


def list_attempts(run_id: int) -> list[sqlite3.Row]:
    return query("SELECT * FROM attempts WHERE run_id = ? ORDER BY id", (run_id,))


def delete_run(run_id: int) -> None:
    execute("DELETE FROM attempts WHERE run_id = ?", (run_id,))
    execute("DELETE FROM runs WHERE id = ?", (run_id,))


def stats_today() -> dict:
    today = datetime.now(config.TZ).strftime("%Y-%m-%d")
    rows = query(
        "SELECT status, COUNT(*) AS c FROM runs WHERE started_at LIKE ? GROUP BY status",
        (f"{today}%",),
    )
    stats = {"success": 0, "failed": 0, "skipped": 0, "running": 0, "total": 0}
    for row in rows:
        stats[row["status"]] = row["c"]
        stats["total"] += row["c"]
    return stats


def recent_runs(limit: int = 6) -> list[sqlite3.Row]:
    return query(
        "SELECT r.*, t.name AS task_name FROM runs r LEFT JOIN tasks t ON t.id = r.task_id "
        "ORDER BY r.id DESC LIMIT ?",
        (limit,),
    )
