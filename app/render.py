"""渲染与解析层：把「任务行 + 时间」变成一次真实请求。

三件事，按调用顺序：
1. `build_ctx()`      生成占位符上下文（日期、时段、人数、场地…），见下方清单；
2. `render_str/body/headers()`  把模板里的 `{{name}}` 替换成实际值；
3. `get_path()/candidates_from_task()`  解析响应、按顺序生成候选场地。

占位符清单（模板里可直接用）
--------------------------
| 名称 | 例 | 说明 |
| --- | --- | --- |
| `date` / `date_compact` / `date_slash` / `date_cn` | `2026-10-21` / `20261021` / `2026/10/21` / `2026年10月21日` | 目标日期 |
| `weekday` / `weekday_cn` | `3` / `周三` | 星期 |
| `slot_start` / `slot_end` | `17:30` | 时分 |
| `slot_start_full` / `slot_end_full` | `17:30:00` | 时分秒 |
| `datetime_start` / `datetime_end` | `2026-10-21 17:30:00` | 日期+时间 |
| `date_ranges` | `{"start_at":…,"end_at":…}` | **裸 JSON**，不要加引号 |
| `time_ranges` | `[{"start_at":…,"end_at":…}]` | **裸 JSON**，不要加引号 |
| `order_type` / `capacity` / `description` | `single_day` / `2` / `打球` | 下单参数 |
| `venue_order_id` / `venue_id` | `2953` | 当前候选场地 ID |
| `venue_name` / `venue_num` / `slot_id` | — | 候选补充信息 |
| `ts` / `ts_ms` / `uuid` / `nonce` / `rerun` / `now` | — | 时间戳与随机量 |

`{{x}}` 的替换是**类型感知**的：上下文里是数字/对象时输出裸值，是字符串时按 JSON 转义，
所以 `"capacity":"{{capacity}}"` 会变成 `"capacity":2` 而不是 `"2"`。
"""
from __future__ import annotations

import json
import re
import time
import uuid as uuidlib
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

from . import config

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
QUOTED_PLACEHOLDER = re.compile(r'"\s*\{\{\s*([A-Za-z0-9_]+)\s*\}\}\s*"')
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _pad_time(value: str) -> str:
    """'9:00' -> '09:00:00'；'19:00' -> '19:00:00'。"""
    raw = (value or "").strip()
    parts = raw.split(":")
    while len(parts) < 3:
        parts.append("00")
    hh = int(parts[0] or 0)
    mm = int(parts[1] or 0)
    ss = int(float(parts[2] or 0))
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def _hhmm(value: str) -> str:
    return _pad_time(value)[:5]


def _get(obj: Any, key: str, default: Any = "") -> Any:
    """兼容 dict 与 sqlite3.Row（后者不支持 .get）。"""
    try:
        value = obj[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def task_target_date(task: Any, today: Optional[date] = None) -> Optional[date]:
    """任务的目标预约日期。

    新模型直接存 `target_date`（YYYY-MM-DD）；旧数据没有该字段时，
    退回「基准日 + day_offset」换算，保证升级后老任务依然有明确目标。

    `today` 用于测试或补算历史上下文；不传则取本机今天。
    """
    raw = str(_get(task, "target_date", "") or "").strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    try:
        offset = int(_get(task, "day_offset", 0) or 0)
    except (TypeError, ValueError):
        offset = 0
    base = today or datetime.now(config.TZ).date()
    return base + timedelta(days=offset)


def auto_task_name(task: Any) -> str:
    """任务名由「目标日期 + 时段」决定，例如 `2026-10-21 17:30-18:00`。

    名称不由用户填写，所以这里既是显示口径也是落库口径。
    兼容 sqlite3.Row 与 dict。
    """
    target = task_target_date(task)
    start = str(_get(task, "slot_start", "") or "19:00")
    end = str(_get(task, "slot_end", "") or "20:00")
    stamp = target.isoformat() if target else "（未设日期）"
    return f"{stamp} {start}-{end}"


def split_time_ranges(date_str: str, start: str, end: str, step_minutes: int = 0) -> list[dict]:
    """把「日期 + 起止时刻」拆成接口需要的 time_ranges 数组。

    step_minutes 为 0 时输出一整段（多数场馆按整段下单）；
    大于 0 时按该粒度切成多个 15/30/60 分钟的小段。
    """
    start_full = _pad_time(start)
    end_full = _pad_time(end)
    base = datetime.strptime(f"{date_str} {start_full}", "%Y-%m-%d %H:%M:%S")
    stop = datetime.strptime(f"{date_str} {end_full}", "%Y-%m-%d %H:%M:%S")
    if stop <= base:
        stop = base + timedelta(minutes=15)

    step = int(step_minutes or 0)
    if step <= 0:
        return [{"start_at": base.strftime("%Y-%m-%d %H:%M:%S"),
                 "end_at": stop.strftime("%Y-%m-%d %H:%M:%S")}]

    ranges: list[dict] = []
    cursor = base
    while cursor < stop:
        nxt = min(cursor + timedelta(minutes=step), stop)
        ranges.append({"start_at": cursor.strftime("%Y-%m-%d %H:%M:%S"),
                       "end_at": nxt.strftime("%Y-%m-%d %H:%M:%S")})
        cursor = nxt
    return ranges


def build_ctx(task: Any, now: Optional[datetime] = None, extra: Optional[dict] = None) -> dict:
    """把任务行渲染成占位符上下文。

    日期只认 `target_date`（旧数据没有该字段时才退回「今天 + day_offset」）。
    如果按 day_offset 现算，任务放置几天后目标日期会跟着漂移，提交到错误的一天。
    """
    now = now or datetime.now(config.TZ)
    target = task_target_date(task, today=now.date()) or now.date()
    date_str = target.strftime("%Y-%m-%d")

    slot_start_full = _pad_time(str(_get(task, "slot_start", "19:00") or "19:00"))
    slot_end_full = _pad_time(str(_get(task, "slot_end", "20:00") or "20:00"))
    step = int(_get(task, "split_minutes", 0) or 0)
    time_ranges = split_time_ranges(date_str, slot_start_full, slot_end_full, step)

    first_id = str(_get(task, "venue_ids", "") or "").split(",")[0].strip()

    ctx: dict[str, Any] = {
        "date": date_str,
        "date_compact": target.strftime("%Y%m%d"),
        "date_slash": target.strftime("%Y/%m/%d"),
        "date_cn": f"{target.year}年{target.month}月{target.day}日",
        "weekday": target.isoweekday(),
        "weekday_cn": WEEKDAY_CN[target.isoweekday() - 1],
        "slot_start": _hhmm(slot_start_full),
        "slot_end": _hhmm(slot_end_full),
        "slot_start_full": slot_start_full,
        "slot_end_full": slot_end_full,
        "datetime_start": f"{date_str} {slot_start_full}",
        "datetime_end": f"{date_str} {slot_end_full}",
        "date_ranges": {"start_at": date_str, "end_at": date_str},
        "time_ranges": time_ranges,
        "order_type": str(_get(task, "order_type", "single_day") or "single_day"),
        "capacity": int(_get(task, "capacity", 2) or 2),
        "description": str(_get(task, "description", "") or ""),
        "venue_order_id": first_id,
        "ts": int(time.time()),
        "ts_ms": int(time.time() * 1000),
        "uuid": str(uuidlib.uuid4()),
        "nonce": uuidlib.uuid4().hex[:12],
        "rerun": 1,
        "now": now.isoformat(timespec="seconds"),
        "venue_name": "",
        "venue_id": first_id,
        "venue_num": "",
        "slot_id": "",
    }
    if extra:
        ctx.update({k: v for k, v in extra.items() if v is not None})
    return ctx


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def render_str(template: str, ctx: dict, missing: Optional[set] = None) -> str:
    if not template:
        return ""

    def repl(match: re.Match) -> str:
        key = match.group(1)
        if key in ctx:
            return _stringify(ctx[key])
        if missing is not None:
            missing.add(key)
        return ""

    return PLACEHOLDER.sub(repl, template)


def render_headers(headers: dict, ctx: dict, missing: Optional[set] = None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in (headers or {}).items():
        rendered = render_str(str(value), ctx, missing)
        out[key] = rendered
    return out


def render_body(body: str, ctx: dict, missing: Optional[set] = None) -> str:
    """类型感知：`"{{venue_id}}"` 在变量为数字时输出 1024（不带引号）。"""
    if not body:
        return ""

    def quoted(match: re.Match) -> str:
        key = match.group(1)
        if key not in ctx:
            if missing is not None:
                missing.add(key)
            return '""'
        value = ctx[key]
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return json.dumps(value)
        if value is None:
            return "null"
        return json.dumps(_stringify(value), ensure_ascii=False)

    replaced = QUOTED_PLACEHOLDER.sub(quoted, body)
    return render_str(replaced, ctx, missing)


# ---------------------------------------------------------------- 字段提取

_SEGMENT = re.compile(r"^([^\[\]]*)((?:\[\d+\])*)$")


def get_path(obj: Any, path: str) -> Any:
    """按 `data.items[0].venueId` 取值；路径为空则原样返回。"""
    if path is None or str(path).strip() == "":
        return obj
    current = obj
    for segment in str(path).split("."):
        segment = segment.strip()
        if not segment:
            continue
        match = _SEGMENT.match(segment)
        if not match:
            return None
        name, indices = match.group(1), match.group(2)
        if name:
            if isinstance(current, dict):
                current = current.get(name)
            else:
                return None
        for index in re.findall(r"\[(\d+)\]", indices):
            if isinstance(current, list) and int(index) < len(current):
                current = current[int(index)]
            else:
                return None
        if current is None:
            return None
    return current


def candidates_from_task(task: Any) -> list[dict]:
    """按任务里配置的场地 ID 顺序生成候选（无需查询接口即可下单）。"""
    raw_ids = str(_get(task, "venue_ids", "") or "").split(",")
    raw_names = str(_get(task, "venues", "") or "").split(",")
    ids = [s.strip() for s in raw_ids if s.strip()]
    names = [s.strip() for s in raw_names if s.strip()]
    return [
        {
            "venue_order_id": vid,
            "venue_id": vid,
            "venue_name": names[idx] if idx < len(names) else f"场地 {vid}",
            "priority": idx,
        }
        for idx, vid in enumerate(ids)
    ]


def build_extras(raw: str) -> list[dict]:
    """解析任务里的「额外变量」配置，容忍尾逗号等手误。"""
    if not raw or not str(raw).strip():
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, dict) and item.get("name") and item.get("expr") is not None:
            out.append({"name": str(item["name"]).strip(), "expr": str(item["expr"])})
    return out


def collect_missing(templates: Iterable[str], ctx: dict) -> set:
    missing: set = set()
    for text in templates:
        render_str(text or "", ctx, missing)
    return missing
