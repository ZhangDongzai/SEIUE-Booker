"""调度与执行引擎：时钟校准 → 预热解析 → 到点提交 → 判定 → 重试 → 留痕。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Optional

import httpx
import jmespath

from . import config, crypto, oauth, passport, render, store, tokeninfo

log = logging.getLogger("booker.runner")

HTTP_STATUS_HINT = {
    401: "会话可能已失效（401），请在「设置」中重新导入浏览器 cURL。",
    403: "被拒绝（403），通常是缺少 CSRF/签名头、Referer 不匹配，或登录态过期。",
    302: "被重定向（302），通常是未登录被踢回登录页，请更新会话。",
    429: "请求过于频繁（429），请调大重试间隔，避免触发风控。",
}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, config.TZ).isoformat(timespec="milliseconds")


def _parse_hhmmss(value: str) -> tuple[int, int, int]:
    parts = (value or "00:00:00").split(":")
    while len(parts) < 3:
        parts.append("0")
    try:
        hh, mm, ss = int(parts[0] or 0), int(parts[1] or 0), int(float(parts[2] or 0))
    except ValueError:
        hh, mm, ss = 0, 0, 0
    return max(0, min(23, hh)), max(0, min(59, mm)), max(0, min(59, ss))


class Engine:
    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._offset = 0.0          # 站点时间 - 本机时间（秒）
        self._offset_at = 0.0
        self._offset_ok = False
        self._workers: dict[int, asyncio.Task] = {}
        self._versions: dict[int, int] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._last_planned: dict[int, float] = {}
        self._next_try: dict[int, float] = {}   # 窗口内失败后的下次重试时刻（站点时间）
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._started = False
        self._keeper: Optional[asyncio.Task] = None

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(config.HTTP_TIMEOUT),
            "follow_redirects": False,
            "http2": False,
            "trust_env": False,          # 不读取系统代理，避免环境变量里的代理串味
            "limits": httpx.Limits(max_keepalive_connections=10, max_connections=20),
            "headers": {"Accept-Language": "zh-CN,zh;q=0.9"},
        }
        if config.PROXY:
            kwargs["proxy"] = config.PROXY
        self._client = httpx.AsyncClient(**kwargs)
        await self.calibrate()
        await self.ensure_token()          # 启动先尝试一次续期（有 RT 且将过期时）
        self.sync_workers()
        self._keeper = asyncio.create_task(self._token_keeper(), name="token-keeper")
        self._started = True
        log.info("engine started, clock offset=%.3fs, proxy=%s", self._offset, config.PROXY or "direct")

    async def stop(self) -> None:
        for worker in list(self._workers.values()):
            worker.cancel()
        self._workers.clear()
        if self._keeper is not None:
            self._keeper.cancel()
            self._keeper = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._started = False

    # ------------------------------------------------------------ 令牌自动续期

    async def _token_keeper(self) -> None:
        """常驻巡检：令牌快过期就换，换完落盘，失败只记状态不刷屏。"""
        while True:
            try:
                await asyncio.sleep(max(30, config.TOKEN_CHECK_SECONDS))
                if self._client is None:
                    continue
                info = tokeninfo.inspect(store.session_headers())
                if not info.get("present"):
                    continue                      # 走 Cookie 会话的部署不适用
                if info.get("exp") is None:
                    continue
                if oauth.needs_refresh(info.get("remain_seconds")):
                    log.info("token remaining %ss, refreshing", info.get("remain_seconds"))
                    await self.ensure_token(force=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("token keeper error")

    async def ensure_token(self, force: bool = False) -> dict:
        """确保 Bearer 可用。

        返回 {ok, refreshed, message, ...}，不抛异常——调用方按 ok 决定后续动作。
        规则：
        - 没配 refresh_token：直接返回 ok=False（不打扰，界面里提示去配）；
        - 剩余有效期 > 阈值且 force=False：什么都不做，ok=True；
        - 换到新 token 后立刻拉一次场地清单校验，200 才算续期成功；
          校验失败则**保留原 token**并回报原因，避免把可用的令牌换成坏令牌。
        """
        headers = store.session_headers()
        info = tokeninfo.inspect(headers)
        rt = store.get_refresh_token()
        passport_cookie = store.get_passport_cookie()

        if not headers:
            return {"ok": False, "refreshed": False, "need_session": True,
                    "message": "尚未导入会话，先粘贴一次 cURL"}
        if not info.get("present"):
            return {"ok": True, "refreshed": False,
                    "message": "当前用 Cookie 会话，无需续期"}
        if not (rt or passport_cookie):
            return {"ok": False, "refreshed": False, "need_setup": True,
                    "message": "未配置免密换令牌或 refresh_token，无法自动续期"}
        if not force and not info.get("expired") and not oauth.needs_refresh(info.get("remain_seconds")):
            return {"ok": True, "refreshed": False,
                    "message": f"令牌剩余 {info.get('remain_seconds')}s，暂不需要续期","remain_seconds": info.get("remain_seconds")}
        if self._client is None:
            return {"ok": False, "refreshed": False, "message": "引擎未启动，无法续期"}

        # 优先走免密换令牌：本站点 Web 端自己不保存 refresh_token，
        # 前端用的就是 passport 会话 + client_id 这条通道，更贴近站点真实行为。
        try:
            if passport_cookie:
                data = await passport.mint(self._client, passport_cookie)
            else:
                data = await oauth.exchange(self._client, rt)
        except (oauth.RefreshError, passport.PassportError) as exc:
            store.set_token_refresh_state({"at": _iso(time.time()), "ok": False,
                                           "message": str(exc),
                                           "status": getattr(exc, "status", 0)})
            return {"ok": False, "refreshed": False, "message": str(exc)}

        new_headers = dict(headers)
        # 保留原有大小写形式，避免个别网关对头名敏感
        key = next((k for k in new_headers if k.lower() == "authorization"), "authorization")
        new_headers[key] = f"Bearer {data['access_token']}"

        # 换完立刻用新令牌拉一次场地清单；不通就不落盘
        verify_status, verify_error = 0, ""
        try:
            resp = await self._client.get(config.VENUES_URL, headers=new_headers,
                                          timeout=max(5.0, config.HTTP_TIMEOUT))
            verify_status = resp.status_code
            if resp.status_code >= 400:
                verify_error = resp.text[:200]
        except httpx.HTTPError as exc:
            verify_error = f"{type(exc).__name__}: {exc}"

        if verify_status and not (200 <= verify_status < 300):
            message = f"新令牌校验失败（HTTP {verify_status}）：{verify_error or '无响应体'}，已保留旧令牌"
            store.set_token_refresh_state({"at": _iso(time.time()), "ok": False, "message": message,
                                           "status": verify_status})
            return {"ok": False, "refreshed": False, "message": message, "verify_status": verify_status}

        store.set_session_headers(new_headers)
        # 服务端可能借这次响应轮换会话 cookie，回写新值，会话才能一直滚动
        rotated_cookie = str(data.get("set_cookie") or "")
        cookie_rotated = False
        if passport_cookie and rotated_cookie and rotated_cookie != passport_cookie:
            store.set_passport_cookie(rotated_cookie)
            cookie_rotated = True
            log.info("passport session cookie rotated")
        new_rt = str(data.get("refresh_token") or rt)
        rotated = (not passport_cookie) and (new_rt != rt)
        if not passport_cookie:
            store.set_refresh_token(new_rt)   # RT 可能被轮换，无条件回写
        new_info = tokeninfo.inspect(new_headers)
        state = {
            "at": _iso(time.time()),
            "ok": True,
            "message": f"续期成功，新令牌剩余 {new_info.get('remain_seconds', '?')}s"
                       + (f"（校验 HTTP {verify_status}）" if verify_status else "（未校验）"),
            "expires_in": data.get("expires_in"),
            "exp": new_info.get("exp"),
            "rotated": rotated,
            "cookie_rotated": cookie_rotated,
            "via": "passport" if passport_cookie else "refresh_token",
        }
        store.set_token_refresh_state(state)
        log.info("token refreshed via %s, exp=%s rotated=%s",
                 state["via"], state["exp"], state["rotated"])
        return {"ok": True, "refreshed": True, "message": state["message"],
                "exp": state["exp"], "remain_seconds": new_info.get("remain_seconds"),
                "rotated": rotated, "verify_status": verify_status}

    def token_status(self) -> dict:
        """给页面用的汇总：令牌状态 + 续期配置 + 上次续期结果。"""
        info = tokeninfo.inspect(store.session_headers())
        rt = store.get_refresh_token()
        pc = store.get_passport_cookie()
        return {
            "token": info,
            "has_refresh_token": bool(rt),
            "refresh_token_mask": crypto.mask_value(rt, 8) if rt else "",
            "has_passport_cookie": bool(pc),
            "passport_cookie_mask": crypto.mask_value(pc, 6) if pc else "",
            "passport_url": config.PASSPORT_AUTHORIZE_URL,
            "passport_client_id": config.PASSPORT_WEB_CLIENT_ID,
            "state": store.token_refresh_state(),
            "skew": config.TOKEN_REFRESH_SKEW,
            "check_seconds": config.TOKEN_CHECK_SECONDS,
            "auto": bool(rt or pc) and info.get("present") and info.get("exp") is not None,
        }

    # ------------------------------------------------------------ 时钟

    def server_now(self) -> float:
        return time.time() + self._offset

    def now_local(self) -> datetime:
        return datetime.now(config.TZ)

    async def calibrate(self) -> None:
        if self._client is None:
            return
        try:
            resp = await self._client.head(config.SITE + "/", timeout=5.0)
            date_header = resp.headers.get("date")
            if not date_header:
                return
            server_ts = parsedate_to_datetime(date_header).timestamp()
            # 用响应前后中点削弱 RTT 影响
            self._offset = server_ts - time.time()
            self._offset_at = time.time()
            self._offset_ok = True
            log.info("clock calibrated: offset=%.3fs", self._offset)
        except Exception as exc:  # 网络异常不应影响主流程
            log.warning("clock calibration failed: %s", exc)

    async def _maybe_recalibrate(self) -> None:
        if time.time() - self._offset_at > 600:
            await self.calibrate()

    def offset_info(self) -> dict:
        return {
            "offset_ms": round(self._offset * 1000, 1),
            "calibrated": self._offset_ok,
            "age_s": round(time.time() - self._offset_at, 1) if self._offset_ok else None,
            "server_time": datetime.fromtimestamp(self.server_now(), config.TZ).strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ------------------------------------------------------------ 调度

    def next_occurrence(self, task: Any, allow_catchup: bool = True) -> Optional[float]:
        """下一次该执行的时间戳（站点时间）；None 表示不再执行。

        执行模型（v1.7）：任务只认一个「目标日期」。
          · 目标日期已过            → 不再执行
          · 目标日期在可预约窗口内  → 立刻执行（失败过则等重试时刻）
          · 目标日期还没进窗口      → 等到窗口开启那天（target - MAX_ORDER_DAYS）的放号时刻
        """
        target = render.task_target_date(task)
        if target is None:
            return None

        today = self.now_local().date()
        if target < today:
            return None

        now = self.server_now()
        task_id = int(task["id"])
        window_end = today + timedelta(days=config.MAX_ORDER_DAYS)

        if target <= window_end:
            # 现在就能约：立刻执行；上次没成功则等重试时刻
            return max(now, self._next_try.get(task_id, 0.0))

        # 还没进窗口：等窗口开启日的放号时刻（默认 00:00:00）
        open_day = target - timedelta(days=config.MAX_ORDER_DAYS)
        hh, mm, ss = _parse_hhmmss(str(task["open_time"]))
        fire = datetime.combine(open_day, datetime.min.time(), tzinfo=config.TZ)
        fire = fire.replace(hour=hh, minute=mm, second=ss)
        return max(fire.timestamp() + self._offset, now)

    def preview_next(self, task: Any) -> Optional[dict]:
        """页面展示用：下一次执行时间（站点时间 + 本机倒数秒数）。"""
        try:
            ts = self.next_occurrence(task)
        except Exception:
            return None
        if ts is None:
            return None
        return {
            "server_time": datetime.fromtimestamp(ts, config.TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "epoch_ms": int((ts - self._offset) * 1000),
            "in_seconds": round(ts - self.server_now(), 1),
        }

    def sync_workers(self) -> None:
        """按当前任务表启停 worker；停用的任务由 worker 自行退出。"""
        for tid, worker in list(self._workers.items()):
            if worker.done():
                self._workers.pop(tid, None)
        for task in store.list_tasks():
            tid = int(task["id"])
            if not int(task["enabled"] or 0):
                self._versions[tid] = self._versions.get(tid, 0) + 1
                continue
            existing = self._workers.get(tid)
            if existing is None or existing.done():
                self._spawn(tid)

    def refresh_task(self, task_id: int) -> None:
        """任务被修改/删除后调用：让旧 worker 失效，启用则立即重建。"""
        task_id = int(task_id)
        self._versions[task_id] = self._versions.get(task_id, 0) + 1
        task = store.get_task(task_id)
        if task is not None and int(task["enabled"] or 0):
            self._spawn(task_id)
        else:
            self._workers.pop(task_id, None)

    def _spawn(self, task_id: int) -> None:
        # 可能被同步路由（线程池）调用：此时没有运行中的事件循环，转回主循环执行
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._spawn, task_id)
            else:
                log.warning("no running loop, worker for task %s not started", task_id)
            return
        version = self._versions.get(task_id, 0) + 1
        self._versions[task_id] = version
        worker = asyncio.create_task(self._worker(task_id, version), name=f"task-{task_id}-v{version}")
        self._workers[task_id] = worker

    def _is_stale(self, task_id: int, version: int) -> bool:
        return self._versions.get(task_id, 0) != version

    async def _worker(self, task_id: int, version: int) -> None:
        log.info("worker start task=%s v=%s", task_id, version)
        while not self._is_stale(task_id, version):
            task = store.get_task(task_id)
            if task is None or not int(task["enabled"] or 0):
                return
            try:
                planned = self.next_occurrence(task)
            except Exception as exc:
                log.error("task %s next_occurrence error: %s", task_id, exc)
                await asyncio.sleep(30)
                continue
            if planned is None:
                log.info("task %s has no future occurrence, worker exit", task_id)
                return

            prewarm = max(0, int(task["prewarm_ms"] or 0)) / 1000.0
            await self._sleep_until_server(planned - prewarm)
            if self._is_stale(task_id, version):
                return

            await self._maybe_recalibrate()
            self._last_planned[task_id] = planned
            try:
                await self._execute(task, planned, "schedule")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("task %s execute error: %s", task_id, exc)
                await asyncio.sleep(3)

    async def _sleep_until_server(self, ts_server: float) -> None:
        while True:
            remain = ts_server - self.server_now()
            if remain <= 0:
                return
            await asyncio.sleep(min(remain, 30.0))

    async def _sleep_until_precise(self, ts_server: float) -> None:
        target_local = ts_server - self._offset
        while True:
            remain = target_local - time.time()
            if remain <= 0:
                return
            if remain > 0.5:
                await asyncio.sleep(min(remain - 0.3, 5.0))
            elif remain > 0.03:
                await asyncio.sleep(remain - 0.02)
            else:
                await asyncio.sleep(0.001)

    # ------------------------------------------------------------ 执行

    async def trigger(self, task_id: int) -> Optional[int]:
        task = store.get_task(task_id)
        if task is None:
            raise RuntimeError("任务不存在")
        lock = self._locks.setdefault(task_id, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("该任务正在执行中，请稍后再试")
        return await self._execute(task, None, "manual")

    async def _execute(self, task: Any, planned_server: Optional[float], trigger: str) -> Optional[int]:
        task_id = int(task["id"])
        lock = self._locks.setdefault(task_id, asyncio.Lock())
        if lock.locked():
            log.info("task %s busy, skip %s", task_id, trigger)
            return None
        async with lock:
            started = self.server_now()
            run_id = store.create_run(task, trigger, _iso(planned_server or started), _iso(started))
            resolved: dict[str, Any] = {"task": task["name"], "trigger": trigger}
            missing_all: set[str] = set()
            seq = 0
            status, message = "failed", ""
            try:
                ctx = render.build_ctx(task, self.now_local())
                extras = render.build_extras(str(task["extras"] or "[]"))
                list_tpl = store.get_template(task["list_template_id"])

                # 任务里配置的场地 ID 是白名单：批次/顺序都由它决定，绝不越界下单
                candidates = render.candidates_from_task(task)
                resolved["candidates_from_config"] = candidates[:30]

                if list_tpl is not None:
                    info = await self._call(list_tpl, ctx)
                    seq += 1
                    missing_all.update(info["missing"])
                    self._save_attempt(run_id, seq, "（查询）", info, ok=True)
                    resolved["list"] = {"status": info["status"], "url": info["url"],
                                        "error": info["error"], "missing": info["missing"]}
                    if info["json"] is not None:
                        for extra in extras:
                            value = render.get_path(info["json"], extra["expr"])
                            if value is not None:
                                ctx[extra["name"]] = value
                else:
                    resolved["list"] = {"note": "未绑定查询模板，直接按任务里配置的场地 ID 提交"}

                # 用场地清单补齐元信息（是否需要审批、窗口天数）
                meta = {str(v.get("id")): v for v in store.get_venues()}
                for cand in candidates:
                    key = str(cand.get("venue_order_id") or cand.get("venue_id") or "")
                    info_meta = meta.get(key)
                    if info_meta:
                        cand["need_approval"] = bool(info_meta.get("need_approval"))
                        cand["max_order_days"] = info_meta.get("max_order_days")

                if not candidates:
                    raise RuntimeError("任务里还没有勾选场地，请到任务编辑页勾选目标场地（或填写场地 ID）")

                # 执行前先确保令牌可用：将过期则换新，换到新令牌立刻校验可用性
                refresh = await self.ensure_token()
                resolved["token_refresh"] = refresh
                token = tokeninfo.inspect(store.session_headers())
                resolved["token"] = {"exp": token.get("exp"), "expired": token.get("expired", False),
                                     "remain_seconds": token.get("remain_seconds")}
                if token.get("present") and token.get("expired"):
                    hint = ""
                    if refresh.get("need_setup"):
                        hint = "；未配置 refresh_token，自动续期不可用"
                    elif refresh.get("message"):
                        hint = "；自动续期也失败了：" + str(refresh["message"])
                    raise RuntimeError(
                        "Bearer 令牌已过期（exp=%s），请在「设置」里重新导入 cURL 后再执行%s"
                        % (token.get("exp"), hint)
                    )
                if not store.session_headers():
                    raise RuntimeError("尚未导入会话，请先在「设置」导入浏览器 cURL")

                if trigger == "schedule" and planned_server is not None:
                    await self._sleep_until_precise(planned_server - 0.2)

                if int(task["dry_run"] or 0) == 1:
                    labels = "、".join(str(c.get("venue_name")) for c in candidates[:5])
                    status = "success"
                    message = f"演练模式：将依次尝试 {len(candidates)} 个场地（{labels}），未提交下单请求"
                    return run_id

                order_tpl = store.get_template(task["order_template_id"])
                if order_tpl is None:
                    raise RuntimeError("未绑定下单模板")

                rounds = max(1, int(task["max_attempts"] or 1))
                interval = max(0, int(task["interval_ms"] or 0)) / 1000.0
                deadline = time.time() + config.MAX_RUN_SECONDS
                success = False

                for rnd in range(1, rounds + 1):
                    if time.time() > deadline:
                        message = "已超过单轮硬性时限（120s），停止重试"
                        break
                    if rnd > 1:
                        await asyncio.sleep(interval)
                        if list_tpl is not None:
                            info = await self._call(list_tpl, ctx)
                            seq += 1
                            missing_all.update(info["missing"])
                            self._save_attempt(run_id, seq, "（复检）", info, ok=True)
                            if info["json"] is not None:
                                for extra in extras:
                                    value = render.get_path(info["json"], extra["expr"])
                                    if value is not None:
                                        ctx[extra["name"]] = value

                    for cand in candidates:
                        if time.time() > deadline:
                            break
                        call_ctx = dict(ctx)
                        call_ctx["rerun"] = rnd
                        venue_label = str(cand.get("venue_name") or "")
                        order_venue_id = str(cand.get("venue_order_id") or cand.get("venue_id") or "")
                        call_ctx["venue_name"] = venue_label
                        call_ctx["venue_id"] = order_venue_id
                        call_ctx["venue_order_id"] = order_venue_id
                        for key in ("slot_id", "venue_num"):
                            if cand.get(key) not in (None, ""):
                                call_ctx[key] = cand[key]

                        info = await self._call(order_tpl, call_ctx)
                        seq += 1
                        missing_all.update(info["missing"])
                        ok = self._judge(task, info)
                        self._save_attempt(run_id, seq, venue_label or "未指定场地", info, ok=ok)
                        if ok:
                            success = True
                            tail = "（该场地需要审批，请到「我的预约」确认状态）" if cand.get("need_approval") else ""
                            message = f"第 {rnd} 轮提交成功：{venue_label or '未指定场地'}{tail}"
                            break
                        message = self._fail_message(info, rnd, task)
                    if success:
                        break

                status = "success" if success else "failed"
                resolved["rounds"] = rounds
                return run_id
            except Exception as exc:
                if isinstance(exc, RuntimeError):
                    # 业务性中断（未配置/令牌过期/无场地）：直接用人话记录，不必打堆栈
                    message = str(exc)
                    log.warning("run %s aborted: %s", run_id, exc)
                else:
                    message = f"执行异常：{type(exc).__name__}: {exc}"
                    log.exception("run %s failed", run_id)
                status = "failed"
                return run_id
            finally:
                resolved["missing_placeholders"] = sorted(missing_all)
                store.finish_run(run_id, status, message or "执行结束", resolved)
                # 目标日期只有一天：真约到就停用任务；其余情况（失败、演练）隔一会儿再来。
                # 注意：必须给每一种结果都安排下一次，否则窗口内会无限次立即重跑。
                if status == "success" and int(task["dry_run"] or 0) == 0:
                    self._next_try.pop(task_id, None)
                    store.set_task_enabled(task_id, False)
                    log.info("task %s done (success), auto-disabled", task_id)
                else:
                    self._next_try[task_id] = self.server_now() + config.RETRY_SECONDS
                    log.info("task %s finished (%s), next try in %ss",
                             task_id, status, config.RETRY_SECONDS)

    def _judge(self, task: Any, info: dict) -> bool:
        status = int(info["status"] or 0)
        mode = str(task["success_mode"] or "http2xx")
        expression = str(task["success_expr"] or "").strip()
        payload = info["json"]

        # 框架级异常即使 2xx 也不算成功（blink 用 name/message 表达错误）
        if isinstance(payload, dict) and str(payload.get("name") or "").startswith("blink\\"):
            return False
        if payload is None and info["text"] and not info["text"].lstrip().startswith(("<", "{")):
            return False

        if mode == "expr" and expression:
            if payload is None:
                return False
            try:
                return bool(jmespath.search(expression, payload))
            except Exception as exc:
                log.warning("success expr error: %s", exc)
                return False
        return 200 <= status < 300

    def _fail_message(self, info: dict, rnd: int, task: Any) -> str:
        status = int(info["status"] or 0)
        hint = HTTP_STATUS_HINT.get(status, "")
        base = f"第 {rnd} 轮未成功：HTTP {status}"
        payload = info.get("json")
        if isinstance(payload, dict):
            detail = payload.get("message") or payload.get("msg") or payload.get("error")
            if detail:
                base += f"（{detail}）"
        if info["error"]:
            base += f"（{info['error']}）"
        if hint:
            base += f" —— {hint}"
        return base

    def _save_attempt(self, run_id: int, seq: int, venue: str, info: dict, ok: bool) -> None:
        try:
            store.add_attempt(
                run_id, seq,
                venue_name=venue,
                method=info.get("method", ""),
                url=info.get("url", ""),
                request_headers=json.dumps(info.get("request_headers", {}), ensure_ascii=False, indent=2),
                request_body=info.get("body", "") or "",
                http_status=int(info.get("status") or 0),
                duration_ms=float(info.get("duration_ms") or 0),
                ok=ok,
                response_excerpt=(info.get("text") or "")[:8000],
                error=info.get("error") or "",
            )
        except Exception as exc:
            log.warning("save attempt failed: %s", exc)

    async def _call(self, tpl: Any, ctx: dict) -> dict:
        assert self._client is not None
        missing: set[str] = set()
        url = render.render_str(str(tpl["url"]), ctx, missing)
        try:
            headers = json.loads(tpl["headers"] or "{}")
        except ValueError:
            headers = {}
        rendered_headers = render.render_headers(headers, ctx, missing)
        body = render.render_body(str(tpl["body"] or ""), ctx, missing)

        merged = dict(store.session_headers())
        for key, value in rendered_headers.items():
            # 身份类头以导入的会话为准，避免模板里残留的旧令牌覆盖新令牌
            if key.lower() in config.SESSION_AUTHORITATIVE and any(
                existing.lower() == key.lower() for existing in merged
            ):
                continue
            merged[key] = value
        if body and not any(k.lower() == "content-type" for k in merged):
            merged["Content-Type"] = "application/json"

        method = str(tpl["method"] or "GET").upper()
        started = time.perf_counter()
        try:
            request = self._client.build_request(
                method, url, headers=merged, content=body.encode("utf-8") if body else None
            )
            response = await self._client.send(request)
            duration = (time.perf_counter() - started) * 1000
            text = response.text
            try:
                payload = response.json()
            except ValueError:
                payload = None
            return {
                "status": response.status_code,
                "json": payload,
                "text": text,
                "method": method,
                "url": url,
                "request_headers": merged,
                "body": body,
                "duration_ms": duration,
                "error": "",
                "missing": sorted(missing),
            }
        except Exception as exc:
            duration = (time.perf_counter() - started) * 1000
            return {
                "status": 0,
                "json": None,
                "text": "",
                "method": method,
                "url": url,
                "request_headers": merged,
                "body": body,
                "duration_ms": duration,
                "error": f"{type(exc).__name__}: {exc}",
                "missing": sorted(missing),
            }

    async def check_session(self, url: str) -> dict:
        """用当前导入的会话头访问一个地址，判断登录态是否仍然有效。"""
        assert self._client is not None
        started = time.perf_counter()
        headers = store.session_headers()
        try:
            response = await self._client.get(url, headers=headers)
            return {
                "status": response.status_code,
                "text": response.text[:4000],
                "error": "",
                "duration_ms": (time.perf_counter() - started) * 1000,
            }
        except Exception as exc:
            return {
                "status": 0,
                "text": "",
                "error": f"{type(exc).__name__}: {exc}",
                "duration_ms": (time.perf_counter() - started) * 1000,
            }

    async def fetch_venues(self, url: str = "") -> dict:
        """拉取场地清单（只读 GET），用于界面里的场地选择与元信息。"""
        assert self._client is not None
        target = (url or config.VENUES_URL).strip()
        started = time.perf_counter()
        try:
            response = await self._client.get(target, headers=store.session_headers())
            payload = None
            try:
                payload = response.json()
            except ValueError:
                pass
            rows: list[dict] = []
            if isinstance(payload, list):
                source = payload
            elif isinstance(payload, dict):
                source = payload.get("data") if isinstance(payload.get("data"), list) else []
            else:
                source = []
            for item in source:
                if not isinstance(item, dict) or item.get("id") is None:
                    continue
                rows.append({
                    "id": item.get("id"),
                    "name": str(item.get("name") or ""),
                    "description": str(item.get("description") or ""),
                    "need_approval": bool(item.get("need_approval")),
                    "max_order_days": item.get("max_order_days"),
                    "time_limit": item.get("time_limit"),
                    "open_status": item.get("open_status"),
                    "open_time_ranges": item.get("open_time_ranges") or [],
                })
            return {
                "status": response.status_code,
                "rows": rows,
                "error": "",
                "text": response.text[:600],
                "duration_ms": (time.perf_counter() - started) * 1000,
            }
        except Exception as exc:
            return {"status": 0, "rows": [], "error": f"{type(exc).__name__}: {exc}",
                    "text": "", "duration_ms": (time.perf_counter() - started) * 1000}

    async def probe(self, tpl: Any) -> dict:
        """用示例上下文试发一次查询模板（仅允许 list 类型，避免误下单）。"""
        if str(tpl["kind"]) != "list":
            raise RuntimeError("为避免误下单，仅支持探测查询类模板；下单请用任务的演练模式验证")
        sample_task = {"day_offset": 7, "slot_start": "19:00", "slot_end": "20:00"}
        ctx = render.build_ctx(sample_task, self.now_local(), {
            "venue_id": 1, "venue_name": "1号场", "slot_id": 1, "venue_num": 1,
        })
        return await self._call(tpl, ctx)
