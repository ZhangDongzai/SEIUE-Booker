"""Web 层：页面路由、表单处理、鉴权与生命周期。"""
from __future__ import annotations

import asyncio
import faulthandler
import json
import logging
import re
import sys
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config, crypto, curlparse, passport, preset, render, store, tokeninfo
from .webcore import (App, HTMLResponse, Jinja2Templates, JSONResponse,
                      RedirectResponse, Request, StaticFiles)
from .runner import Engine

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("booker.web")

BASE = Path(__file__).resolve().parent
engine = Engine()
COOKIE_NAME = "booker_auth"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(crypto.app_secret(), salt="booker-login")


async def _loop_monitor() -> None:
    """盯事件循环：一旦被卡住超过 5 秒，就把所有线程栈打进日志。

    手机上「页面一直转圈」通常是事件循环被同步操作卡死，
    没有这个探针就只能猜；有了它，booker.log 里会直接指出卡在哪一行。
    """
    while True:
        started = time.perf_counter()
        await asyncio.sleep(2)
        drift = (time.perf_counter() - started) - 2.0
        if drift > 5.0:
            log.warning("事件循环卡顿 %.1fs，打印线程栈", drift)
            try:
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            except Exception:
                pass


@asynccontextmanager
async def lifespan(_: App):
    store.init_db()
    preset.ensure_default_templates()
    await engine.start()
    monitor = asyncio.create_task(_loop_monitor(), name="loop-monitor")
    log.info("web ready on port %s, data_dir=%s", config.APP_PORT, config.DATA_DIR)
    try:
        yield
    finally:
        monitor.cancel()
        await engine.stop()


app = App(title=config.APP_NAME, version=config.APP_VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

templates = Jinja2Templates(directory=str(BASE / "views"))
templates.env.globals.update(app_name=config.APP_NAME, version=config.APP_VERSION,
                             tz_name=config.TZ_NAME,
                             display_name=render.auto_task_name)


def page(name: str, request: Request, **ctx) -> HTMLResponse:
    payload = {
        "request": request,
        "page": ctx.pop("page", ""),
        "ok": request.query_params.get("ok", ""),
        "err": request.query_params.get("err", ""),
        "auth_enabled": bool(config.APP_PASSWORD),
        "set_session": bool(store.session_headers()),
        **ctx,
    }
    return templates.TemplateResponse(request=request, name=name, context=payload)


def redirect(path: str, ok: str = "", err: str = "") -> RedirectResponse:
    sep = "&" if "?" in path else "?"
    if ok:
        path = f"{path}{sep}ok={quote(ok)}"
    elif err:
        path = f"{path}{sep}err={quote(err)}"
    return RedirectResponse(path, status_code=303)


def ph_ctx() -> dict:
    """占位符文本由后端生成，避免在 Jinja 模板里拼 {{ }} 造成解析歧义。"""
    return {
        "ph_date": "{{date}}",
        "ph_venue_id": "{{venue_order_id}}",
        "ph_venue_name": "{{venue_name}}",
        "ph_slot_start": "{{slot_start}}",
        "ph_slot_id": "{{slot_id}}",
        "ph_ts": "{{ts}}",
        "ph_csrf": "{{csrf}}",
        "ph_url_example": config.API + "/scms/venue/order-venues/{{venue_order_id}}/orders",
        "ph_body_example": ('{"type":"{{order_type}}","time_ranges":{{time_ranges}},'
                            '"date_ranges":{{date_ranges}},"capacity":"{{capacity}}",'
                            '"description":"{{description}}"}'),
    }


# ---------------------------------------------------------------- 鉴权

@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    if not config.APP_PASSWORD or path.startswith("/static") or path in ("/login", "/healthz"):
        return await call_next(request)
    token = request.cookies.get(COOKIE_NAME, "")
    try:
        _serializer().loads(token, max_age=7 * 24 * 3600)
        return await call_next(request)
    except (BadSignature, SignatureExpired):
        if path.startswith("/api"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse(f"/login?next={quote(path)}", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    if not config.APP_PASSWORD:
        return RedirectResponse("/", status_code=303)
    return page("login.html", request, page="login", next=next)


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    password = str(form.get("password") or "")
    next_path = str(form.get("next") or "/")
    if password != config.APP_PASSWORD:
        return redirect("/login", err="口令不正确")
    response = RedirectResponse(next_path if next_path.startswith("/") else "/", status_code=303)
    response.set_cookie(COOKIE_NAME, _serializer().dumps("ok"), httponly=True,
                        samesite="lax", max_age=7 * 24 * 3600)
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": config.APP_VERSION}


# ---------------------------------------------------------------- 仪表盘

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    tasks = store.list_tasks()
    rows = []
    for task in tasks:
        rows.append({
            "task": task,
            "next": engine.preview_next(task) if int(task["enabled"] or 0) else None,
            "last": store.query_one(
                "SELECT * FROM runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task["id"],)
            ),
        })
    return page(
        "dashboard.html", request, page="dashboard",
        stats=store.stats_today(),
        rows=rows,
        runs=store.recent_runs(6),
        offset=engine.offset_info(),
        session=store.session_headers(),
        token=tokeninfo.inspect(store.session_headers()),
        token_text=tokeninfo.describe(tokeninfo.inspect(store.session_headers())),
        last_check=store.last_check(),
        venues_count=len(store.get_venues()),
        refresh=engine.token_status(),
        check_url=store.get_setting("check_url", config.DEFAULT_CHECK_URL),
    )


# ---------------------------------------------------------------- 任务

@app.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request):
    tpls = {t["id"]: t for t in store.list_templates()}
    tasks = store.list_tasks()
    rows = []
    for task in tasks:
        rows.append({
            "task": task,
            "next": engine.preview_next(task) if int(task["enabled"] or 0) else None,
            "list_tpl": tpls.get(task["list_template_id"]),
            "order_tpl": tpls.get(task["order_template_id"]),
        })
    return page("tasks.html", request, page="tasks", rows=rows)


@app.get("/tasks/new", response_class=HTMLResponse)
def task_new(request: Request):
    today = datetime.now(config.TZ).date()
    return page("task_form.html", request, page="tasks", task=store.task_form_defaults(),
                templates_list=store.list_templates(), venues=store.get_venues(),
                venues_at=store.venues_synced_at(),
                is_new=True,
                today=today.isoformat(),
                max_date=(today + timedelta(days=60)).isoformat(),
                window_days=config.MAX_ORDER_DAYS)


@app.get("/tasks/{task_id}/edit", response_class=HTMLResponse)
def task_edit(request: Request, task_id: int):
    task = store.get_task(task_id)
    if task is None:
        return redirect("/tasks", err="任务不存在")
    today = datetime.now(config.TZ).date()
    return page("task_form.html", request, page="tasks", task=dict(task),
                templates_list=store.list_templates(), venues=store.get_venues(),
                venues_at=store.venues_synced_at(),
                is_new=False,
                today=today.isoformat(),
                max_date=(today + timedelta(days=60)).isoformat(),
                window_days=config.MAX_ORDER_DAYS)


def _int(value: Any, default: int = 0, lo: Optional[int] = None, hi: Optional[int] = None) -> int:
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        number = default
    if lo is not None:
        number = max(lo, number)
    if hi is not None:
        number = min(hi, number)
    return number


def _normalize_time(value: str, default: str) -> str:
    raw = (value or "").strip() or default
    parts = raw.split(":")
    while len(parts) < 3:
        parts.append("00")
    try:
        hh, mm, ss = int(parts[0] or 0), int(parts[1] or 0), int(float(parts[2] or 0))
    except ValueError:
        return default
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def _normalize_hhmm(value: str, default: str) -> str:
    raw = (value or "").strip() or default
    match = re.search(r"(\d{1,2}):(\d{2})", raw)
    if not match:
        return default
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def _resolve_venue_ids(form, existing: str = "") -> tuple[str, str]:
    """按勾选顺序得到场地 ID 串；返回 (id 串, 名称串)。"""
    checked = [str(v).strip() for v in form.getlist("venue_ids") if str(v).strip()]
    if checked:
        ids = checked
    elif not store.get_venues():
        # 还没同步场地清单时页面上没有勾选框，保留原有配置，避免编辑时被清空
        ids = [s.strip() for s in existing.split(",") if s.strip()]
    else:
        ids = []
    seen: set[str] = set()
    ids = [i for i in ids if not (i in seen or seen.add(i))]
    names = {str(v.get("id")): str(v.get("name") or "") for v in store.get_venues()}
    labels = [names.get(i) or f"场地 {i}" for i in ids]
    return ",".join(ids), ",".join(labels)


@app.post("/tasks/save")
async def task_save(request: Request):
    form = await request.form()
    task_id = _int(form.get("id"), 0)

    current = store.get_task(task_id) if task_id else None
    existing = dict(current) if current is not None else {}
    venue_ids, venue_names = _resolve_venue_ids(form, str(existing.get("venue_ids") or ""))

    slot_start = _normalize_hhmm(str(form.get("slot_start") or ""), "19:00")
    slot_end = _normalize_hhmm(str(form.get("slot_end") or ""), "20:00")
    back = f"/tasks/{task_id}/edit" if task_id else "/tasks/new"

    # 目标日期是固定的一天，不再用「偏移天数」推算
    today = datetime.now(config.TZ).date()
    raw_date = str(form.get("target_date") or "").strip()
    try:
        target = date.fromisoformat(raw_date)
    except ValueError:
        return redirect(back, err="请选择目标日期（格式 2026-10-21）")
    if target < today:
        return redirect(back, err=f"目标日期 {target.isoformat()} 已经过去，请选今天或之后的日期")

    open_time = _normalize_time(str(form.get("open_time") or ""), "00:00:30")

    payload = {
        # 名称由「目标日期 + 时段」推导，用户不填
        "name": render.auto_task_name({"target_date": target.isoformat(),
                                       "slot_start": slot_start, "slot_end": slot_end}),
        "enabled": 1 if form.get("enabled") else 0,
        "target_date": target.isoformat(),
        "day_offset": max(0, (target - today).days),   # 仅作老字段兼容
        "open_time": open_time,
        # schedule_mode / once_at 已停用，保留空值以兼容旧库
        "schedule_mode": "daily",
        "once_at": "",
        "venues": venue_names,
        "venue_ids": venue_ids,
        "slot_start": slot_start,
        "slot_end": slot_end,
        "split_minutes": 0,
        "order_type": "single_day",
        "capacity": _int(form.get("capacity"), 2, 1, 20),
        "description": str(form.get("description") or "").strip(),
        "list_template_id": _int(form.get("list_template_id"), 0) or None,
        "order_template_id": _int(form.get("order_template_id"), 0) or None,
        # 「高级」项不再暴露：编辑时沿用原值，新建用默认
        "extras": str(existing.get("extras") or "[]"),
        "success_mode": str(existing.get("success_mode") or "http2xx"),
        "success_expr": str(existing.get("success_expr") or ""),
        "max_attempts": _int(form.get("max_attempts"), 5, 1, 50),
        "interval_ms": _int(form.get("interval_ms"), 300, 0, 60000),
        "prewarm_ms": _int(form.get("prewarm_ms"), 8000, 0, 120000),
        "dry_run": 1 if form.get("dry_run") else 0,
    }

    saved_id = store.save_task(payload, task_id or None)
    engine.refresh_task(saved_id)

    days = (target - today).days
    if days <= config.MAX_ORDER_DAYS:
        note = "目标日期已在可预约窗口内，启用后立即执行"
    else:
        open_day = target - timedelta(days=config.MAX_ORDER_DAYS)
        note = (f"目标日期还没进窗口，将在 {open_day.isoformat()} {open_time} 自动开始执行")
    enabled = bool(payload["enabled"])
    return redirect("/tasks", ok="任务已保存" + ("；" + note if enabled else "（未启用）"))


@app.post("/tasks/{task_id}/toggle")
async def task_toggle(task_id: int):
    task = store.get_task(task_id)
    if task is None:
        return redirect("/tasks", err="任务不存在")
    new_value = 0 if int(task["enabled"] or 0) else 1
    store.execute("UPDATE tasks SET enabled = ?, updated_at = ? WHERE id = ?",
                  (new_value, store.now_iso(), task_id))
    engine.refresh_task(task_id)
    return redirect("/tasks", ok="已启用" if new_value else "已停用")


@app.post("/tasks/{task_id}/delete")
async def task_delete(task_id: int):
    store.delete_task(task_id)
    engine.refresh_task(task_id)
    return redirect("/tasks", ok="任务已删除")


@app.post("/tasks/{task_id}/run")
async def task_run(task_id: int):
    try:
        run_id = await engine.trigger(task_id)
    except RuntimeError as exc:
        return redirect("/tasks", err=str(exc))
    if run_id is None:
        return redirect("/tasks", err="任务正在执行中，已跳过")
    return redirect(f"/runs/{run_id}", ok="已执行，查看下方记录")


# ---------------------------------------------------------------- 模板库

@app.get("/templates", response_class=HTMLResponse)
def templates_page(request: Request):
    return page("templates.html", request, page="templates",
                rows=store.list_templates(),
                used={r["list_template_id"] for r in store.list_tasks()} |
                     {r["order_template_id"] for r in store.list_tasks()})


@app.get("/templates/new", response_class=HTMLResponse)
def template_new(request: Request, kind: str = "list"):
    return page("template_form.html", request, page="templates",
                tpl={"id": None, "name": "", "kind": "order" if kind == "order" else "list",
                     "method": "POST" if kind == "order" else "GET",
                     "url": "", "headers": "{}", "body": "", "note": ""},
                is_new=True, probe=None, **ph_ctx())


@app.get("/templates/{tpl_id}/edit", response_class=HTMLResponse)
def template_edit(request: Request, tpl_id: int):
    tpl = store.get_template(tpl_id)
    if tpl is None:
        return redirect("/templates", err="模板不存在")
    data = dict(tpl)
    try:
        data["headers"] = json.dumps(json.loads(data["headers"] or "{}"), ensure_ascii=False, indent=2)
    except ValueError:
        pass
    return page("template_form.html", request, page="templates", tpl=data,
                is_new=False, probe=None, **ph_ctx())


@app.post("/templates/parse")
async def template_parse(request: Request):
    """把粘贴的 cURL 解析后回填到表单（不落库）。"""
    form = await request.form()
    raw = str(form.get("raw") or "")
    kind = "order" if str(form.get("kind")) == "order" else "list"
    try:
        parsed = curlparse.parse_curl(raw)
    except ValueError as exc:
        return redirect(f"/templates/new?kind={kind}", err=str(exc))
    data = {
        "id": None,
        "name": str(form.get("name") or "").strip(),
        "kind": kind,
        "method": parsed.method,
        "url": parsed.url,
        "headers": json.dumps(parsed.headers, ensure_ascii=False, indent=2),
        "body": curlparse.pretty_body(parsed.body),
        "note": "\n".join(parsed.warnings),
    }
    return page("template_form.html", request, page="templates", tpl=data,
                is_new=True, probe=None, parsed=True, warnings=parsed.warnings, **ph_ctx())


@app.post("/templates/save")
async def template_save(request: Request):
    form = await request.form()
    tpl_id = _int(form.get("id"), 0)
    headers_raw = str(form.get("headers") or "{}").strip() or "{}"
    try:
        headers = json.loads(headers_raw)
        if not isinstance(headers, dict):
            raise ValueError
        headers_raw = json.dumps(headers, ensure_ascii=False)
    except ValueError:
        back = f"/templates/{tpl_id}/edit" if tpl_id else f"/templates/new?kind={form.get('kind','list')}"
        return redirect(back, err="请求头必须是 JSON 对象")

    url = str(form.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return redirect(f"/templates/{tpl_id}/edit" if tpl_id else "/templates/new", err="URL 不合法")

    payload = {
        "name": str(form.get("name") or "").strip() or "未命名模板",
        "kind": "order" if str(form.get("kind")) == "order" else "list",
        "method": str(form.get("method") or "GET").upper(),
        "url": url,
        "headers": headers_raw,
        "body": str(form.get("body") or ""),
        "note": str(form.get("note") or ""),
    }
    store.save_template(payload, tpl_id or None)
    return redirect("/templates", ok="模板已保存")


@app.post("/templates/{tpl_id}/delete")
async def template_delete(tpl_id: int):
    affected = [t["id"] for t in store.list_tasks()
                if t["list_template_id"] == tpl_id or t["order_template_id"] == tpl_id]
    store.delete_template(tpl_id)
    for task_id in affected:
        engine.refresh_task(task_id)
    return redirect("/templates", ok="模板已删除（引用它的任务已解绑）")


@app.post("/templates/{tpl_id}/probe", response_class=HTMLResponse)
async def template_probe(request: Request, tpl_id: int):
    tpl = store.get_template(tpl_id)
    if tpl is None:
        return redirect("/templates", err="模板不存在")
    data = dict(tpl)
    try:
        data["headers"] = json.dumps(json.loads(data["headers"] or "{}"), ensure_ascii=False, indent=2)
    except ValueError:
        pass
    try:
        result = await engine.probe(tpl)
        probe = {
            "status": result["status"],
            "url": result["url"],
            "duration_ms": round(result["duration_ms"], 1),
            "error": result["error"],
            "text": (result["text"] or "")[:20000],
        }
        try:
            probe["pretty"] = json.dumps(result["json"], ensure_ascii=False, indent=2)[:20000]
        except (TypeError, ValueError):
            probe["pretty"] = ""
    except RuntimeError as exc:
        probe = {"error": str(exc), "status": 0, "text": "", "pretty": "", "url": "", "duration_ms": 0}
    return page("template_form.html", request, page="templates", tpl=data,
                is_new=False, probe=probe, **ph_ctx())


# ---------------------------------------------------------------- 记录

@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request):
    return page("runs.html", request, page="runs", rows=store.list_runs(120))


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int):
    run = store.get_run(run_id)
    if run is None:
        return redirect("/runs", err="记录不存在")
    attempts = store.list_attempts(run_id)
    resolved = {}
    try:
        resolved = json.loads(run["resolved"] or "{}")
    except ValueError:
        pass
    deviation = "—"
    if run["planned_at"] and run["started_at"]:
        try:
            planned = datetime.fromisoformat(run["planned_at"])
            started = datetime.fromisoformat(run["started_at"])
            deviation = f"{(started - planned).total_seconds() * 1000:+.0f} ms"
        except ValueError:
            pass
    return page("run_detail.html", request, page="runs", run=run,
                attempts=attempts, resolved=resolved, deviation=deviation)


@app.post("/runs/{run_id}/delete")
def run_delete(run_id: int):
    store.delete_run(run_id)
    return redirect("/runs", ok="记录已删除")


# ---------------------------------------------------------------- 设置

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    headers = store.session_headers()
    token = tokeninfo.inspect(headers)
    exp_text = "—"
    if token.get("exp"):
        exp_text = datetime.fromtimestamp(float(token["exp"]), config.TZ).strftime("%Y-%m-%d %H:%M")
    return page("settings.html", request, page="settings",
                headers=crypto.mask_headers(headers),
                header_count=len(headers),
                token=token,
                token_text=tokeninfo.describe(token),
                token_exp_text=exp_text,
                offset=engine.offset_info(),
                check_url=store.get_setting("check_url", config.DEFAULT_CHECK_URL),
                venues_url=config.VENUES_URL,
                last_check=store.last_check(),
                venues=store.get_venues(),
                venues_at=store.venues_synced_at(),
                refresh=engine.token_status(),
                token_url=config.OAUTH_TOKEN_URL,
                data_dir=str(config.DATA_DIR))


@app.post("/settings/refresh-now")
async def settings_refresh_now(request: Request):
    """立即换一次新令牌，并做场地清单校验。优先走免密换令牌。"""
    result = await engine.ensure_token(force=True)
    if result.get("ok") and result.get("refreshed"):
        return redirect("/settings", ok=result["message"])
    if result.get("ok"):
        return redirect("/settings", ok=result["message"])
    if result.get("need_setup"):
        return redirect("/settings", err="还没配置续期方式：先在下面填入「会话 Cookie」")
    if result.get("need_session"):
        return redirect("/settings", err="还没导入会话，先用上面「一键导入」粘一条 cURL（自动续期需要先有令牌）")
    return redirect("/settings", err=f"续期失败：{result.get('message')}")


async def _auto_sync_and_check() -> tuple[list[str], list[str]]:
    """令牌就绪后自动跑一遍：同步场地清单 + 会话自检。

    返回 (成功说明, 失败说明)，由调用方拼成一条提示。
    """
    notes: list[str] = []
    problems: list[str] = []

    venues = await engine.fetch_venues(config.VENUES_URL)
    if venues.get("rows"):
        store.set_venues(venues["rows"])
        notes.append(f"已同步 {len(venues['rows'])} 个场地")
    else:
        detail = venues.get("error") or f"HTTP {venues.get('status')}"
        problems.append(f"同步场地失败（{detail}）")

    url = str(store.get_setting("check_url") or config.DEFAULT_CHECK_URL)
    try:
        check = await engine.check_session(url)
    except Exception as exc:  # 网络异常也要留痕
        check = {"status": 0, "error": f"{type(exc).__name__}: {exc}",
                 "text": "", "duration_ms": 0}
    store.set_last_check({
        "at": store.now_iso(),
        "url": url,
        "status": check["status"],
        "duration_ms": round(check.get("duration_ms") or 0, 1),
        "error": check.get("error") or "",
        "excerpt": (check.get("text") or "")[:200],
        "ok": 200 <= int(check["status"]) < 300,
    })
    if 200 <= int(check["status"]) < 300:
        notes.append(f"会话自检通过（HTTP {check['status']}）")
    else:
        problems.append(f"会话自检异常（HTTP {check['status']} {check.get('error') or ''}）".strip())
    return notes, problems


@app.post("/settings/passport-cookie")
async def settings_passport_cookie(request: Request):
    """保存 passport 域的会话 cookie（加密落盘）。留空则清除。"""
    form = await request.form()
    raw_in = str(form.get("passport_cookie") or "").strip()
    if raw_in.lower().startswith("bearer "):
        raw_in = raw_in[7:].strip()
    # 归一化：兼容「名=值」「cookie: 名=值」「面板复制的 名<TAB>值」「多行多条」
    raw = passport.normalize_cookie(raw_in)
    if not raw:
        if raw_in:
            return redirect("/settings",
                            err="没识别出有效的 cookie：至少要有一个 名=值，例如 PHPSESSID=xxxx")
        store.set_passport_cookie("")
        return redirect("/settings", ok="已清除会话 Cookie，免密换令牌已关闭")
    if "=" not in raw:
        return redirect("/settings",
                        err="这不像 cookie：至少要有一个 名=值，例如 PHPSESSID=xxxx")
    if len(raw) < 16:
        return redirect("/settings", err="会话 Cookie 看起来太短，请确认复制完整")
    store.set_passport_cookie(raw)

    # 配好就立刻用起来：换新令牌 → 同步场地清单 → 会话自检，一次做完
    notes: list[str] = []
    problems: list[str] = []

    refreshed = await engine.ensure_token(force=True)
    if refreshed.get("ok"):
        notes.append("已换到新令牌" if refreshed.get("refreshed")
                     else str(refreshed.get("message") or "令牌暂不需要续期"))
    else:
        problems.append(f"换令牌失败：{refreshed.get('message')}")

    if refreshed.get("ok") and store.session_headers():
        more_notes, more_problems = await _auto_sync_and_check()
        notes += more_notes
        problems += more_problems

    if problems:
        return redirect("/settings", err="会话 Cookie 已保存；"
                        + "；".join(problems + notes))
    return redirect("/settings", ok="会话 Cookie 已保存；" + "；".join(notes))


@app.post("/settings/session")
async def settings_session(request: Request):
    form = await request.form()
    raw = str(form.get("raw") or "")
    try:
        headers = curlparse.parse_headers_input(raw)
    except ValueError as exc:
        return redirect("/settings", err=f"解析失败：{exc}")
    lower = {k.lower() for k in headers}
    if "cookie" not in lower and "authorization" not in lower:
        return redirect("/settings", err="未识别到 Cookie 或 Authorization，请粘贴包含登录态的 cURL")
    existing = store.session_headers()
    existing.update(headers)
    store.set_session_headers(existing)
    token = tokeninfo.inspect(existing)
    return redirect("/settings", ok=f"会话已更新（{len(existing)} 个请求头，{tokeninfo.describe(token)}）")


@app.post("/settings/quickstart")
async def settings_quickstart(request: Request):
    """一条真实 cURL → 会话 + 下单模板，全部自动配好。"""
    form = await request.form()
    raw = str(form.get("raw") or "")
    try:
        parsed = curlparse.parse_curl(raw)
    except ValueError as exc:
        return redirect("/settings", err=f"解析失败：{exc}")

    session = preset.session_headers_from(parsed)
    if not session.get("authorization") and not any(k.lower() == "cookie" for k in session):
        return redirect("/settings", err="这条 cURL 里没有 Authorization/Cookie，请复制已登录状态下的请求")

    try:
        tpl = preset.build_order_template(parsed)
    except ValueError as exc:
        return redirect("/settings", err=str(exc))

    existing = store.session_headers()
    existing.update(session)
    store.set_session_headers(existing)

    tpl_id = store.save_template({k: tpl[k] for k in ("name", "kind", "method", "url", "headers", "body", "note")})
    if tpl.get("venue_id"):
        store.set_setting("last_venue_id", tpl["venue_id"])

    token = tokeninfo.inspect(existing)
    note = f"已导入会话与下单模板；{tokeninfo.describe(token)}"
    return redirect(f"/templates/{tpl_id}/edit", ok=note)


@app.post("/settings/venues")
async def settings_venues(request: Request):
    form = await request.form()
    url = str(form.get("venues_url") or "").strip() or config.VENUES_URL
    result = await engine.fetch_venues(url)
    if not result["rows"]:
        detail = result["error"] or f"HTTP {result['status']}：{result['text'][:120]}"
        return redirect("/settings", err=f"同步场地失败：{detail}")
    store.set_venues(result["rows"])
    return redirect("/settings", ok=f"已同步 {len(result['rows'])} 个场地")


@app.post("/settings/check")
async def settings_check(request: Request):
    form = await request.form()
    url = str(form.get("check_url") or "").strip() or config.DEFAULT_CHECK_URL
    store.set_setting("check_url", url)
    try:
        result = await engine.check_session(url)
    except Exception as exc:  # 网络异常也要留痕
        result = {"status": 0, "error": f"{type(exc).__name__}: {exc}", "text": "", "duration_ms": 0}
    store.set_last_check({
        "at": store.now_iso(),
        "url": url,
        "status": result["status"],
        "duration_ms": round(result["duration_ms"], 1),
        "error": result["error"],
        "excerpt": (result["text"] or "")[:200],
        "ok": 200 <= int(result["status"]) < 300,
    })
    info = store.last_check()
    if 200 <= int(result["status"]) < 300:
        return redirect("/settings", ok=f"会话自检通过：HTTP {result['status']}，耗时 {round(result['duration_ms'])}ms")
    return redirect("/settings", err=f"会话自检异常：HTTP {result['status']} {result['error']}")


@app.post("/settings/calibrate")
async def settings_calibrate():
    await engine.calibrate()
    info = engine.offset_info()
    return redirect("/settings", ok=f"已校准，站点与本机偏移 {info['offset_ms']} ms")
