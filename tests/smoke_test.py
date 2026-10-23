"""端到端冒烟测试：本地起一个假站点，真实跑一遍调度与提交。

覆盖：
  1. cURL 解析 / 占位符渲染 / 请求体形状与线上一致
  2. 一键导入：真实 cURL → 会话 + 下单模板
  3. 页面渲染（仪表盘、任务、模板、设置、执行详情）
  4. 执行引擎：场地优先级回退、成功判定、演练不下单、令牌过期拦截
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import parse_qsl, unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TZ = ZoneInfo("Asia/Shanghai")
PASSED = 0
FAILED: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}  {extra}")


# ---------------------------------------------------------------- 假站点

RECORD: dict = {"calls": []}
RESULTS: dict = {}
# 续期相关的可控状态
OAUTH: dict = {"mode": "ok", "next_rt": "RT-ROTATED-abcdefghijklmnop", "last_form": {}, "last_ct": ""}
# 免密换令牌（passport 会话 cookie）的可控状态
PASSPORT: dict = {"mode": "ok", "last_cookie": "", "last_form": {}}

VENUES = [
    {"id": 2953, "name": "羽毛球场地13号", "need_approval": True, "max_order_days": 7,
     "time_limit": 1, "open_status": "opened", "description": ""},
    {"id": 2952, "name": "羽毛球场地12号", "need_approval": True, "max_order_days": 7,
     "time_limit": 1, "open_status": "opened", "description": ""},
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    def _json(self, payload: dict, status: int = 200, extra_headers: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _path(self) -> str:
        """httpx 在有代理环境时会发送 absolute-form，这里统一取路径。"""
        raw = self.path
        return urlsplit(raw).path if raw.startswith("http") else raw.split("?", 1)[0]

    def do_GET(self):
        path = self._path()
        RECORD["calls"].append({"method": "GET", "path": path,
                                "headers": dict(self.headers), "body": ""})
        auth = self.headers.get("authorization", "")
        if path.startswith("/scms/venue/order-venues"):
            # 用来模拟「换到的新令牌其实不可用」
            if auth == "Bearer BAD-TOKEN":
                self._json({"name": "blink\\core\\HttpException",
                            "message": "Unauthenticated."}, 401)
                return
            self._json(VENUES)
            return
        self._json({"ok": True})

    def do_POST(self):
        path = self._path()
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        RECORD["calls"].append({"method": "POST", "path": path,
                                "headers": dict(self.headers), "body": raw})
        # 令牌续期：站点要求表单编码
        if path.endswith("/chalk/oauth/tokens"):
            form = dict(parse_qsl(raw))
            OAUTH["last_form"] = form
            OAUTH["last_ct"] = self.headers.get("Content-Type", "")
            if OAUTH["mode"] == "fail":
                self._json({"error": "invalid_request",
                            "message": "The refresh token is invalid.",
                            "hint": "Cannot decrypt the refresh token"}, 400)
                return
            if OAUTH["mode"] == "bad_verify":
                self._json({"access_token": "BAD-TOKEN", "refresh_token": "RT-AFTER-BAD",
                            "expires_in": 86399, "token_type": "Bearer"})
                return
            self._json({"access_token": fake_jwt(86400), "refresh_token": OAUTH["next_rt"],
                        "expires_in": 86400, "token_type": "Bearer"})
            return
        # 免密换令牌：POST passport/authorize，靠 Cookie 认会话
        # 按真实站点形态返回：HTTP 201、expires_in 是字符串、并轮换会话 cookie
        if path.endswith("/authorize"):
            PASSPORT["last_cookie"] = self.headers.get("Cookie", "")
            PASSPORT["last_form"] = dict(parse_qsl(raw))
            if PASSPORT["mode"] == "fail":
                self._json({"name": "Unauthorized", "message": "Login required"}, 401)
                return
            self._json({"access_token": fake_jwt(86400), "expires_in": "86400",
                        "token_type": "bearer"}, 201,
                       extra_headers={"Set-Cookie":
                                      "PHPSESSID=rotated-session-abcdef0123456789; "
                                      "Path=/; HttpOnly; SameSite=Lax"})
            return
        # 2953 恒定冲突，用于验证自动切换到下一块场地
        if "/2953/" in path:
            self._json({"name": "blink\\core\\HttpException", "message": "该时段已被预约"}, 409)
            return
        self._json({"id": 8888, "status": "approvaling"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


MOCK = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
PORT = MOCK.server_address[1]
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="booker-test-")
os.environ["SITE_BASE"] = f"http://127.0.0.1:{PORT}"
os.environ["API_BASE"] = f"http://127.0.0.1:{PORT}"
os.environ["CHECK_URL"] = f"http://127.0.0.1:{PORT}/scms/venue/order-venues"
# 免密换令牌端点也指向本地假站点
os.environ["PASSPORT_AUTHORIZE_URL"] = f"http://127.0.0.1:{PORT}/authorize"

from app import config, crypto, curlparse, preset, render, store, tokeninfo  # noqa: E402
from app.runner import Engine  # noqa: E402


def fake_jwt(exp_offset: int = 3600, sub: str = "4924846") -> str:
    def seg(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    now = int(time.time())
    return f"{seg({'alg': 'RS256'})}.{seg({'exp': now + exp_offset, 'iat': now - 3600, 'sub': sub, 'aud': '3'})}.sig"


BEARER = fake_jwt()


def bearer_headers() -> dict:
    return {
        "authorization": "Bearer " + BEARER,
        "x-reflection-id": "4205528",
        "x-role": "student",
        "x-school-id": "282",
        "origin": config.SITE,
        "referer": config.SITE + "/",
    }


# ------------------------------------------------ 0. 内核与加密（Termux 关键路径）

def test_core() -> None:
    print("[0] Web 内核 / 加密（无 fastapi、无 cryptography）")

    # --- 加密往返：不同长度、不同字符集 ---
    samples = [
        "a",
        "Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiI0OTI0ODQ2In0.c2ln",
        "中文与 emoji 混排 🏸 测试",
        "x" * 5000,
    ]
    for sample in samples:
        token = crypto.encrypt_text(sample)
        tag = f"len={len(sample)}"
        check(f"密文可解回（{tag}）", crypto.decrypt_text(token) == sample)
        check(f"密文带 b1 前缀（{tag}）", token.startswith(crypto.PREFIX))
        if len(sample) >= 4:   # 单字符明文可能撞巧出现在 base64 里，此项无意义
            check(f"密文不含明文（{tag}）", sample not in token)

    # --- 随机 IV：同明文两次加密结果必须不同 ---
    one, two = crypto.encrypt_text("same-plaintext"), crypto.encrypt_text("same-plaintext")
    check("相同明文两次密文不同", one != two)
    check("两份密文都能解回", crypto.decrypt_text(one) == crypto.decrypt_text(two) == "same-plaintext")

    # --- 完整性：篡改任何一个字节都必须被拒 ---
    token = crypto.encrypt_text("tamper-me")
    body = token[len(crypto.PREFIX):]
    flipped = body[:-2] + ("A" if body[-2] != "A" else "B") + body[-1]
    check("篡改密文被拒", crypto.decrypt_text(crypto.PREFIX + flipped, "FALLBACK") == "FALLBACK")
    check("空串加密仍是空串", crypto.encrypt_text("") == "")
    check("空串解密返回默认值", crypto.decrypt_text("", "D") == "D")
    check("非法 base64 返回默认值", crypto.decrypt_text("b1.!!!", "D") == "D")
    check("截断密文返回默认值", crypto.decrypt_text("b1.AAAA", "D") == "D")
    check("旧版 Fernet 密文不崩溃", crypto.decrypt_text("gAAAAABl0000000000", "D") == "D")

    # --- JSON 往返 ---
    payload = {"headers": {"authorization": "Bearer x"}, "n": 1, "中文": [1, 2], "ok": True}
    check("JSON 往返一致", crypto.decrypt_json(crypto.encrypt_json(payload)) == payload)
    check("JSON 解不开时给默认值", crypto.decrypt_json("b1.zzz", {"d": 1}) == {"d": 1})

    # --- 密钥持久化 ---
    check("密钥可重复读取", crypto.app_secret() == crypto.app_secret())
    check("密钥文件已生成", os.path.exists(config.KEY_PATH))
    with open(config.KEY_PATH, "rb") as fh:
        check("密钥长度为 Fernet 同格式（44 字节）", len(fh.read().strip()) == 44)

    # --- Web 内核：参数注入行为与原 FastAPI 一致 ---
    from app import webcore

    seen: dict = {}

    async def by_path(request, task_id: int):
        seen["task_id"] = task_id
        seen["type"] = type(task_id).__name__
        seen["has_request"] = isinstance(request, webcore.Request)
        return webcore.PlainTextResponse("ok")

    def by_query(request, kind: str = "list"):
        seen["kind"] = kind
        return webcore.PlainTextResponse("ok")

    def bare():
        return webcore.PlainTextResponse("ok")

    probe = webcore.App()
    probe.get("/x/{task_id}")(by_path)
    probe.get("/q")(by_query)
    probe.get("/bare")(bare)

    from starlette.testclient import TestClient as ProbeClient
    with ProbeClient(probe) as pc:
        check("路径参数 handler 正常返回", pc.get("/x/42").text == "ok")
        check("路径参数按注解转成 int", seen.get("task_id") == 42 and seen.get("type") == "int")
        check("request 被注入", seen.get("has_request") is True)
        pc.get("/q?kind=order")
        check("查询参数按名注入", seen.get("kind") == "order")
        pc.get("/q")
        check("查询参数缺失时用默认值", seen.get("kind") == "list")
        check("无参 handler 正常", pc.get("/bare").text == "ok")
        check("未注册路径返回 404", pc.get("/nope").status_code == 404)

    # ---------------- 执行时机：窗口内立即执行，窗口外等窗口开启 ----------------
    from app.runner import Engine as _Engine
    today = datetime.now(config.TZ).date()
    probe_engine = _Engine()
    now_ts = probe_engine.server_now()

    near = (today + timedelta(days=3)).isoformat()
    far = (today + timedelta(days=20)).isoformat()
    gone = (today - timedelta(days=1)).isoformat()

    check("目标日期可解析",
          render.task_target_date({"target_date": near}) == date.fromisoformat(near))
    check("老数据回退到 day_offset",
          render.task_target_date({"target_date": "", "day_offset": 2})
          == today + timedelta(days=2))

    # 任务放置几天后 day_offset 会过时；渲染必须只认 target_date，否则提交到错误的一天
    drift = render.build_ctx(
        {"target_date": "2026-11-05", "day_offset": 0, "slot_start": "17:30",
         "slot_end": "18:30", "split_minutes": 0, "venue_ids": "2953"},
        datetime(2026, 10, 20, 10, 0, tzinfo=config.TZ))
    check("渲染优先 target_date（不被 day_offset 带偏）",
          drift["date"] == "2026-11-05", drift["date"])

    ts_near = probe_engine.next_occurrence({"id": 1, "target_date": near,
                                            "open_time": "00:00:00"})
    check("窗口内 → 立即执行", ts_near is not None and abs(ts_near - now_ts) < 5, str(ts_near))

    ts_far = probe_engine.next_occurrence({"id": 2, "target_date": far,
                                          "open_time": "00:00:00"})
    expect_far = datetime.combine(
        date.fromisoformat(far) - timedelta(days=config.MAX_ORDER_DAYS),
        datetime.min.time(), tzinfo=config.TZ).timestamp()
    check("窗口外 → 等窗口开启日",
          ts_far is not None and abs(ts_far - expect_far) < 60, f"{ts_far} vs {expect_far}")

    check("日期已过 → 不再执行",
          probe_engine.next_occurrence({"id": 3, "target_date": gone,
                                        "open_time": "00:00:00"}) is None)

    # 失败后的重试时刻生效
    probe_engine._next_try[4] = now_ts + 300
    ts_retry = probe_engine.next_occurrence({"id": 4, "target_date": near,
                                             "open_time": "00:00:00"})
    check("失败后按重试时刻再跑", ts_retry is not None and abs(ts_retry - (now_ts + 300)) < 5,
          str(ts_retry))


# ---------------------------------------------------------------- 1. 解析与渲染

def test_parse_and_render() -> None:
    print("[1] cURL 解析 / 占位符 / 请求体")
    raw = (
        "curl --url 'https://api.seiue.com/scms/venue/order-venues/2953/orders' \\\n"
        "-H 'accept: application/json, text/plain, */*' \\\n"
        "-H ':authority: api.seiue.com' \\\n"
        "-H 'content-type: application/json' \\\n"
        "-b 'SESSION=abc123' \\\n"
        "--data-raw '{\"type\":\"single_day\",\"capacity\":2}'"
    )
    parsed = curlparse.parse_curl(raw)
    check("解析出 URL", parsed.url.endswith("/order-venues/2953/orders"), parsed.url)
    check("解析出方法", parsed.method == "POST", parsed.method)
    check("忽略 HTTP/2 伪头", not any(k.startswith(":") for k in parsed.headers))
    check("Cookie 来自 -b", "SESSION=abc123" in parsed.headers.get("Cookie", ""))

    task = {
        "day_offset": 7, "slot_start": "17:30", "slot_end": "18:30", "split_minutes": 0,
        "venue_ids": "2953", "capacity": 2, "description": "打球", "order_type": "single_day",
    }
    ctx = render.build_ctx(task, datetime(2026, 10, 16, 10, 0, tzinfo=TZ))
    check("目标日期 = 当天 + 偏移", ctx["date"] == "2026-10-23", ctx["date"])
    check("date_ranges 形态正确",
          ctx["date_ranges"] == {"start_at": "2026-10-23", "end_at": "2026-10-23"},
          str(ctx["date_ranges"]))
    check("time_ranges 整段",
          ctx["time_ranges"] == [{"start_at": "2026-10-23 17:30:00",
                                  "end_at": "2026-10-23 18:30:00"}],
          str(ctx["time_ranges"]))

    body = ('{"type":"{{order_type}}","time_ranges":{{time_ranges}},'
            '"date_ranges":{{date_ranges}},"capacity":"{{capacity}}",'
            '"description":"{{description}}"}')
    rendered = render.render_body(body, ctx)
    obj = json.loads(rendered)
    check("type 渲染", obj["type"] == "single_day", str(obj.get("type")))
    check("capacity 输出数字而非字符串", obj["capacity"] == 2 and isinstance(obj["capacity"], int))
    check("time_ranges 是数组", isinstance(obj["time_ranges"], list) and len(obj["time_ranges"]) == 1)
    check("中文备注不被转义", "打球" in rendered)

    parts = render.split_time_ranges("2026-10-23", "17:30", "18:30", 15)
    check("按 15 分钟拆分", len(parts) == 4, str(len(parts)))
    check("拆分首段正确", parts[0]["start_at"] == "2026-10-23 17:30:00")

    cands = render.candidates_from_task({"venue_ids": "2953,2952", "venues": "场地13,场地12"})
    check("按配置生成候选", [c["venue_order_id"] for c in cands] == ["2953", "2952"])


def test_quickstart() -> None:
    print("[2] 一键导入：cURL → 会话 + 模板")
    body = json.dumps({
        "type": "single_day",
        "time_ranges": [{"start_at": "2026-10-23 17:30:00", "end_at": "2026-10-23 17:45:00"}],
        "date_ranges": {"start_at": "2026-10-23", "end_at": "2026-10-23"},
        "capacity": 2,
        "description": "打球",
    }, ensure_ascii=False)
    curl = " ".join([
        "curl --url 'https://api.seiue.com/scms/venue/order-venues/2953/orders'",
        "-H 'accept: application/json, text/plain, */*'",
        "-H 'authorization: Bearer demo.demo.demo'",
        "-H 'content-type: application/json'",
        "-H 'x-role: student'",
        "-H 'x-school-id: 282'",
        f"--data-raw '{body}'",
    ])
    parsed = curlparse.parse_curl(curl)
    tpl = preset.build_order_template(parsed)
    check("URL 场地 ID 占位符化", tpl["url"].endswith("/order-venues/{{venue_order_id}}/orders"), tpl["url"])
    check("提取出场地 ID", tpl["venue_id"] == "2953", tpl["venue_id"])
    check("Authorization 移出模板",
          not any(k.lower() == "authorization" for k in json.loads(tpl["headers"])))
    check("保留 x-school-id", json.loads(tpl["headers"]).get("x-school-id") == "282")
    # 模板体里 time_ranges/date_ranges 是裸占位符，整体不是合法 JSON，按文本断言
    check("type 已占位符化", '"type": "{{order_type}}"' in tpl["body"], tpl["body"][:80])
    check("time_ranges 为裸占位符", tpl["body"].count("{{time_ranges}}") == 1)
    check("date_ranges 为裸占位符", tpl["body"].count("{{date_ranges}}") == 1)
    check("capacity 已占位符化", '"capacity": "{{capacity}}"' in tpl["body"])
    check("description 已占位符化", "{{description}}" in tpl["body"])

    session = preset.session_headers_from(parsed)
    check("会话含 authorization", "authorization" in session)
    check("会话含上下文头", "x-role" in session and "x-school-id" in session)

    try:
        preset.build_order_template(curlparse.parse_curl("curl --url 'https://a.b/c' -X OPTIONS"))
        check("OPTIONS 被拒绝", False)
    except ValueError as exc:
        check("OPTIONS 被拒绝", "预检" in str(exc), str(exc))

    info = tokeninfo.inspect({"authorization": "Bearer " + BEARER})
    check("令牌可解析", info.get("present") and not info.get("expired"), str(info))
    check("令牌剩余时间合理", 3000 < (info.get("remain_seconds") or 0) <= 3700, str(info.get("remain_seconds")))
    expired = tokeninfo.inspect({"authorization": "Bearer " + fake_jwt(-10)})
    check("过期令牌可识别", expired.get("expired") is True)
    check("无令牌时 present=False", tokeninfo.inspect({}).get("present") is False)


# ---------------------------------------------------------------- 3. 页面

def test_pages() -> None:
    print("[3] 页面与表单")
    from starlette.testclient import TestClient
    from app.main import app

    RECORD["calls"].clear()
    with TestClient(app) as client:
        check("仪表盘 200", client.get("/").status_code == 200)
        kinds = [t["kind"] for t in store.list_templates()]
        check("只预置场地清单模板", kinds == ["list"], str(kinds))
        check("不再预置下单模板", "order" not in kinds, str(kinds))
        dash = client.get("/").text
        check("未导入会话时有引导", "导入会话" in dash and "cURL" in dash)
        check("令牌格显示未导入", "未导入" in dash)

        # 同步场地清单
        resp = client.post("/settings/venues", data={}, follow_redirects=False)
        check("同步场地重定向", resp.status_code == 303, str(resp.status_code))
        rows = store.get_venues()
        check("场地已缓存", len(rows) == 2, str(len(rows)))
        check("场地元信息含审批标记", rows[0].get("need_approval") is True)

        page = client.get("/tasks/new").text
        check("场地可勾选", 'name="venue_ids"' in page and "羽毛球场地13号" in page)
        check("含新字段 capacity", 'name="capacity"' in page)
        # 本轮精简：名称自动生成；拆分粒度 / 下单类型 / 高级项 / 占位符栏都已移除
        check("任务名称不再可填", 'name="name"' not in page)
        check("名称标注为自动生成", "自动生成" in page)
        check("已移除拆分粒度", 'name="split_minutes"' not in page)
        check("已移除下单类型", 'name="order_type"' not in page)
        check("已移除额外变量高级项", 'name="extras"' not in page)
        check("已移除成功判定高级项", 'name="success_mode"' not in page)
        check("已移除占位符复制栏", "可点击复制占位符" not in page)
        check("改为具体日期输入", 'type="date"' in page and 'name="target_date"' in page)
        check("已移除场地优先级", 'name="venue_priority"' not in page)
        check("窗口开启时刻默认 00:00:30",
              'name="open_time"' in page and 'value="00:00:30"' in page)
        check("已移除日期偏移", 'name="day_offset"' not in page)
        check("已移除排程模式", 'name="schedule_mode"' not in page)
        check("已移除单次执行时间", 'name="once_at"' not in page)

        # 一键导入
        body = json.dumps({"type": "single_day", "time_ranges": [], "date_ranges": {},
                           "capacity": 2, "description": "打球"})
        curl = " ".join([
            "curl --url 'https://api.seiue.com/scms/venue/order-venues/2953/orders'",
            "-H 'authorization: Bearer demo.demo.demo'",
            f"--data-raw '{body}'",
        ])
        resp = client.post("/settings/quickstart", data={"raw": curl}, follow_redirects=False)
        check("一键导入重定向", resp.status_code == 303, str(resp.status_code))
        check("会话已写入", "authorization" in store.session_headers())
        check("下单模板已生成", any(t["kind"] == "order" and "{{venue_order_id}}" in t["url"]
                                   for t in store.list_templates()))

        # 保存任务：目标日期是具体的一天
        target_date = (datetime.now(TZ) + timedelta(days=3)).strftime("%Y-%m-%d")
        resp = client.post("/tasks/save", data={
            "enabled": "on", "open_time": "00:00:05", "target_date": target_date,
            "venue_ids": "2953", "slot_start": "17:30", "slot_end": "18:30",
            "capacity": "2", "description": "打球",
            "max_attempts": "3", "interval_ms": "200", "prewarm_ms": "0",
            "list_template_id": "", "order_template_id": str(store.list_templates(kind="order")[0]["id"]),
            "dry_run": "on",
        }, follow_redirects=False)
        check("保存任务 303", resp.status_code == 303, str(resp.status_code))
        task = store.list_tasks()[0]
        check("场地 ID 入库", task["venue_ids"] == "2953", task["venue_ids"])
        check("场地名自动推导", task["venues"] == "羽毛球场地13号", task["venues"])
        check("时段入库", (task["slot_start"], task["slot_end"]) == ("17:30", "18:30"))
        check("人数入库", int(task["capacity"]) == 2)
        check("目标日期入库", task["target_date"] == target_date, str(task["target_date"]))
        check("任务名 = 目标日期 + 时段",
              str(task["name"]) == f"{target_date} 17:30-18:30", str(task["name"]))
        check("拆分粒度固定整段", int(task["split_minutes"] or 0) == 0)
        check("下单类型固定实测值", task["order_type"] == "single_day", str(task["order_type"]))
        check("任务页含场地名", "羽毛球场地13号" in client.get("/tasks").text)

        # 模板下拉默认选中已有的那一个（新建任务时）
        new_page = client.get("/tasks/new").text
        order_id = str(store.list_templates(kind="order")[0]["id"])
        list_id = str(store.list_templates(kind="list")[0]["id"])
        check("下单模板默认选中已有",
              re.search(rf'<option value="{order_id}"\s+selected', new_page) is not None)
        check("场地清单模板默认选中已有",
              re.search(rf'<option value="{list_id}"\s+selected', new_page) is not None)

        # 勾选顺序即尝试顺序（原来的「场地优先级」文本框已移除）
        client.post("/tasks/save", data={
            "enabled": "on", "open_time": "00:00:30", "target_date": target_date,
            "venue_ids": ["2952", "2953"],
            "slot_start": "17:30", "slot_end": "18:30", "max_attempts": "1",
            "interval_ms": "100", "prewarm_ms": "0",
            "order_template_id": str(store.list_templates(kind="order")[0]["id"]),
        }, follow_redirects=False)
        task2 = [t for t in store.list_tasks() if t["venue_ids"] == "2952,2953"][0]
        check("按勾选顺序记录场地", task2["venue_ids"] == "2952,2953", task2["venue_ids"])

        check("模板页 200", client.get("/templates").status_code == 200)
        check("设置页含一键导入", "一键导入" in client.get("/settings").text)


# ---------------------------------------------------------------- 4. 执行

def expected_body(date_str: str) -> dict:
    return {
        "type": "single_day",
        "time_ranges": [{"start_at": f"{date_str} 17:30:00", "end_at": f"{date_str} 18:30:00"}],
        "date_ranges": {"start_at": date_str, "end_at": date_str},
        "capacity": 2,
        "description": "打球",
    }


def test_execution() -> None:
    print("[4] 执行引擎")
    import asyncio

    store.set_session_headers(bearer_headers())
    # 指向假站点的下单模板；预置模板已取消，没有就自建一份，避免打到真实域名
    order_tpl = next((t for t in store.list_templates(kind="order")
                      if t["url"].startswith(config.API)), None)
    if order_tpl is None:
        new_id = store.save_template({
            "name": "测试用下单模板",
            "kind": "order",
            "method": "POST",
            "url": config.API + "/scms/venue/order-venues/{{venue_order_id}}/orders",
            "headers": json.dumps({"content-type": "application/json"}, ensure_ascii=False),
            "body": ('{"type":"{{order_type}}","time_ranges":{{time_ranges}},'
                     '"date_ranges":{{date_ranges}},"capacity":"{{capacity}}",'
                     '"description":"{{description}}"}'),
            "note": "smoke test 自建",
        })
        order_tpl = store.get_template(new_id)

    async def run_case() -> None:
        engine = Engine()
        await engine.start()
        try:
            target = (datetime.now(TZ) + timedelta(days=7)).strftime("%Y-%m-%d")

            # 4.1 场地优先级回退：2953 冲突 → 2952 成功
            store.save_task({
                "name": "回退用例", "enabled": 1, "schedule_mode": "daily", "open_time": "00:00:00",
                "once_at": "", "day_offset": 7, "venues": "", "venue_ids": "2953,2952",
                "slot_start": "17:30", "slot_end": "18:30", "split_minutes": 0,
                "order_type": "single_day", "capacity": 2, "description": "打球",
                "list_template_id": None, "order_template_id": order_tpl["id"],
                "map_items": "", "map_venue_name": "", "map_venue_id": "",
                "map_venue_num": "", "map_slot_start": "", "map_slot_end": "", "map_slot_id": "",
                "map_available": "", "map_available_true": "", "extras": "[]",
                "success_mode": "http2xx", "success_expr": "", "max_attempts": 1,
                "interval_ms": 100, "prewarm_ms": 0, "dry_run": 0,
            })
            tid = store.list_tasks()[0]["id"]
            RECORD["calls"].clear()
            run_id = await engine.trigger(tid)
            run = store.get_run(run_id)
            posts = [c for c in RECORD["calls"] if c["method"] == "POST"]
            check("先打第一块场地", posts and "/2953/" in posts[0]["path"], posts[0]["path"] if posts else "")
            check("失败后自动换下一块", len(posts) == 2 and "/2952/" in posts[1]["path"])
            check("最终判定成功", run["status"] == "success", run["status"] + " " + run["message"])
            check("提示含审批提醒", "审批" in run["message"], run["message"])

            sent = json.loads(posts[1]["body"])
            check("请求体与线上形状一致", sent == expected_body(target), json.dumps(sent, ensure_ascii=False))
            check("身份头来自会话", posts[1]["headers"].get("authorization", "").startswith("Bearer "))
            check("上下文头一并带上", posts[1]["headers"].get("x-school-id") == "282")
            check("Content-Type 保留", posts[1]["headers"].get("content-type") == "application/json")

            attempts = store.list_attempts(run_id)
            check("两次尝试都留痕", len(attempts) == 2, str(len(attempts)))
            check("第一次记为失败", int(attempts[0]["ok"]) == 0 and attempts[0]["http_status"] == 409)
            check("第二次记为成功", int(attempts[1]["ok"]) == 1)
            check("失败原因含接口文案", "已被预约" in attempts[0]["response_excerpt"])
            RESULTS["run_id"] = run_id
            RESULTS["task_id"] = tid

            # 4.2 演练模式不下单
            task = dict(store.get_task(tid))
            task["dry_run"] = 1
            store.save_task(task, tid)
            RECORD["calls"].clear()
            dry_id = await engine.trigger(tid)
            dry = store.get_run(dry_id)
            check("演练不发送 POST", not [c for c in RECORD["calls"] if c["method"] == "POST"])
            check("演练结果成功", dry["status"] == "success", dry["status"])
            check("演练说明含场地数", "2 个场地" in dry["message"], dry["message"])

            # 4.3 令牌过期：直接失败，不发请求
            store.set_session_headers({"authorization": "Bearer " + fake_jwt(-60)})
            task["dry_run"] = 0
            store.save_task(task, tid)
            RECORD["calls"].clear()
            bad_id = await engine.trigger(tid)
            bad = store.get_run(bad_id)
            check("过期令牌判为失败", bad["status"] == "failed", bad["status"])
            check("提示重新导入", "过期" in bad["message"] and "设置" in bad["message"], bad["message"])
            check("过期时不发任何请求", not RECORD["calls"])

            # 4.4 恢复会话，详情页可打开
            store.set_session_headers(bearer_headers())
        finally:
            await engine.stop()

    asyncio.run(run_case())

    from starlette.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        run_id = RESULTS["run_id"]
        resp = client.get(f"/runs/{run_id}")
        check("执行详情页 200", resp.status_code == 200)
        check("详情页含请求轨迹", "请求轨迹" in resp.text)
        check("详情页展示请求体", "time_ranges" in resp.text)


def test_token_refresh() -> None:
    print("[5] 令牌自动续期")
    import asyncio

    from app import oauth
    from app.runner import Engine

    check("阈值内应续期", oauth.needs_refresh(300))
    check("阈值外不应续期", not oauth.needs_refresh(7200))
    check("无法判断时保守续期", oauth.needs_refresh(None))

    # 4.0b 免密换令牌的解析函数（不依赖网络）
    from app import passport as passport_mod
    check("cookie 形态：纯名值", passport_mod._normalize_cookie("PHPSESSID=abc") == "PHPSESSID=abc")
    check("cookie 形态：带头名", passport_mod._normalize_cookie("cookie: PHPSESSID=abc") == "PHPSESSID=abc")
    check("cookie 形态：多条并列", passport_mod._normalize_cookie("a=1; b=2") == "a=1; b=2")
    check("cookie 形态：空白", passport_mod._normalize_cookie("   ") == "")
    # 面板整行复制是制表符分隔的 Name⇥Value
    check("cookie 形态：TSV 名⇥值",
          passport_mod._normalize_cookie("PHPSESSID\tabcdef0123456789") == "PHPSESSID=abcdef0123456789",
          passport_mod._normalize_cookie("PHPSESSID\tabcdef0123456789"))
    # 多条 cookie 各占一行
    check("cookie 形态：多行多条",
          passport_mod._normalize_cookie("PHPSESSID=abc\nlocale=zh") == "PHPSESSID=abc; locale=zh",
          passport_mod._normalize_cookie("PHPSESSID=abc\nlocale=zh"))
    # 整条 Set-Cookie 里的属性段要剔掉
    check("cookie 形态：剔除 Set-Cookie 属性",
          passport_mod._normalize_cookie("PHPSESSID=abc; Path=/; HttpOnly; SameSite=Lax")
          == "PHPSESSID=abc",
          passport_mod._normalize_cookie("PHPSESSID=abc; Path=/; HttpOnly; SameSite=Lax"))
    check("从 JSON 顶层取 token", passport_mod._extract({"access_token": "T"}, "", 200) == "T")
    check("从 data 嵌套取 token", passport_mod._extract({"data": {"access_token": "T"}}, "", 200) == "T")
    check("从纯文本里认 JWT",
          passport_mod._extract(None, "x eyJhbGciOiJSUzI1NiJ9.eyJhYmMifQ.c2lnbmF0dXJl y", 200).startswith("eyJ"))
    check("取不到 token 时返回空", passport_mod._extract({"foo": 1}, "no token here", 200) == "")
    # 服务端轮换会话 cookie 时的合并规则
    check("轮换合并：同名替换", passport_mod._merge_cookie("a=1; b=2", ["b=3; path=/"]) == "a=1; b=3",
          passport_mod._merge_cookie("a=1; b=2", ["b=3; path=/"]))
    check("轮换合并：异名追加", passport_mod._merge_cookie("a=1", ["c=3"]) == "a=1; c=3",
          passport_mod._merge_cookie("a=1", ["c=3"]))
    check("轮换合并：空值视为删除", passport_mod._merge_cookie("a=1; b=2", ["a=; Max-Age=0"]) == "b=2",
          passport_mod._merge_cookie("a=1; b=2", ["a=; Max-Age=0"]))

    # 4.0c 按真实站点形态调 mint：HTTP 201 + expires_in 是字符串 + Set-Cookie 轮换
    import httpx as _httpx

    def _passport_handler(request: _httpx.Request) -> _httpx.Response:
        assert request.url.path.endswith("/authorize")
        assert request.headers.get("cookie") == "PHPSESSID=old-session-0123456789"
        return _httpx.Response(
            201,
            json={"token_type": "bearer", "expires_in": "86400",
                  "access_token": fake_jwt(86400)},
            headers={"Set-Cookie": "PHPSESSID=new-session-9876543210; Path=/; HttpOnly"})

    async def run_mint() -> None:
        async with _httpx.AsyncClient(
                transport=_httpx.MockTransport(_passport_handler)) as client:
            out = await passport_mod.mint(client, "PHPSESSID=old-session-0123456789")
        check("201 也算换令牌成功", out["access_token"].startswith("eyJ"), str(out)[:120])
        check("expires_in 字符串能解析", out["expires_in"] == 86400, str(out.get("expires_in")))
        check("轮换后的 Cookie 被带回",
              out["set_cookie"] == "PHPSESSID=new-session-9876543210", str(out.get("set_cookie")))

    asyncio.run(run_mint())

    def headers_with(token: str) -> dict:
        base = bearer_headers()
        base["authorization"] = "Bearer " + token
        return base

    async def run_case() -> None:
        engine = Engine()
        await engine.start()
        try:
            # 4.0 完全没会话：要求先导入，而不是误报「Cookie 会话」
            store.set_session_headers({})
            store.set_refresh_token("")
            store.set_passport_cookie("")
            res = await engine.ensure_token()
            check("无会话时提示先导入", res.get("need_session") is True, str(res))
            check("无会话时不说 Cookie 会话", "Cookie" not in res.get("message", ""), res.get("message", ""))

            # 4.1 没配 refresh_token
            store.set_session_headers(headers_with(fake_jwt(7200)))
            store.set_refresh_token("")
            res = await engine.ensure_token()
            check("未配 RT 时提示去配置", res.get("need_setup") is True, str(res))

            # 4.2 剩余充足 -> 一个请求都不发
            store.set_refresh_token("RT-ORIGINAL-0123456789abcdef")
            RECORD["calls"].clear()
            res = await engine.ensure_token()
            check("剩余充足时不续期", res["ok"] and not res["refreshed"], str(res))
            check("未发续期请求", not any("oauth/tokens" in c["path"] for c in RECORD["calls"]))

            # 4.3 强制续期成功
            OAUTH["mode"] = "ok"
            old_bearer = tokeninfo.find_bearer(store.session_headers())
            RECORD["calls"].clear()
            res = await engine.ensure_token(force=True)
            check("强制续期成功", res["ok"] and res["refreshed"], str(res))
            form = OAUTH["last_form"]
            check("用表单编码而非 JSON", "form-urlencoded" in OAUTH["last_ct"], OAUTH["last_ct"])
            check("grant_type 正确", form.get("grant_type") == "refresh_token", str(form))
            check("带上 refresh_token", form.get("refresh_token") == "RT-ORIGINAL-0123456789abcdef", str(form))
            check("带上 client 凭据", bool(form.get("client_id")) and bool(form.get("client_secret")))
            check("新令牌已落盘",
                  tokeninfo.find_bearer(store.session_headers()) != old_bearer)
            check("RT 轮换后已回写", store.get_refresh_token() == OAUTH["next_rt"],
                  store.get_refresh_token())
            check("续期后剩余时间充足",
                  (engine.token_status()["token"].get("remain_seconds") or 0) > 80000)
            check("换完做了场地校验",
                  any(c["path"].startswith("/scms/venue/order-venues") and
                      "Bearer BAD" not in c["headers"].get("authorization", "")
                      for c in RECORD["calls"]))
            check("状态记录为成功", store.token_refresh_state().get("ok") is True)

            # 4.4 续期被拒 -> 保留旧令牌
            before = store.session_headers()
            OAUTH["mode"] = "fail"
            res = await engine.ensure_token(force=True)
            check("续期被拒时 ok=False", res.get("ok") is False, str(res))
            check("报错含服务端文案", "refresh token is invalid" in res["message"], res["message"])
            check("失败不覆盖旧令牌", store.session_headers() == before)
            check("状态记录为失败", store.token_refresh_state().get("ok") is False)

            # 4.5 换到了令牌但校验不通 -> 不落盘
            OAUTH["mode"] = "bad_verify"
            before = store.session_headers()
            res = await engine.ensure_token(force=True)
            check("校验失败则整体失败", res.get("ok") is False, str(res))
            check("校验失败提示保留旧令牌", "已保留旧令牌" in res["message"], res["message"])
            check("校验失败不写坏令牌", store.session_headers() == before)
            check("校验失败不覆盖 RT", store.get_refresh_token() == OAUTH["next_rt"])
            OAUTH["mode"] = "ok"

            # 4.5b 只有会话 Cookie（没有 RT）也能续期 —— 站点 Web 端的真实通道
            store.set_refresh_token("")
            store.set_passport_cookie("PHPSESSID=fake-session-cookie-0123456789")
            PASSPORT["mode"] = "ok"
            RECORD["calls"].clear()
            res = await engine.ensure_token(force=True)
            check("免密换令牌成功", res["ok"] and res["refreshed"], str(res))
            check("带上了会话 Cookie",
                  PASSPORT["last_cookie"] == "PHPSESSID=fake-session-cookie-0123456789",
                  PASSPORT["last_cookie"])
            check("form 用 response_type=token",
                  PASSPORT["last_form"].get("response_type") == "token", str(PASSPORT["last_form"]))
            check("form 带上 web client_id",
                  PASSPORT["last_form"].get("client_id") == config.PASSPORT_WEB_CLIENT_ID,
                  str(PASSPORT["last_form"]))
            check("新令牌已落盘",
                  tokeninfo.find_bearer(store.session_headers()) != old_bearer)
            check("没有 RT 就不回写", store.get_refresh_token() == "")
            check("服务端轮换的会话 Cookie 已回写",
                  store.get_passport_cookie() == "PHPSESSID=rotated-session-abcdef0123456789",
                  store.get_passport_cookie())
            check("状态记录 cookie 轮换",
                  store.token_refresh_state().get("cookie_rotated") is True,
                  str(store.token_refresh_state()))
            check("状态记录 via=passport",
                  store.token_refresh_state().get("via") == "passport",
                  str(store.token_refresh_state()))
            check("优先走免密通道而非 oauth",
                  not any("oauth/tokens" in c["path"] for c in RECORD["calls"]))

            # 4.5c 会话 Cookie 失效：明确报错且不覆盖旧令牌
            before = store.session_headers()
            PASSPORT["mode"] = "fail"
            res = await engine.ensure_token(force=True)
            check("Cookie 失效时 ok=False", res.get("ok") is False, str(res))
            check("提示重新复制 Cookie", "Cookie" in res["message"], res["message"])
            check("Cookie 失效不覆盖旧令牌", store.session_headers() == before)
            check("Cookie 失效时 RT 仍为空", store.get_refresh_token() == "")
            PASSPORT["mode"] = "ok"

            # 4.5d 收尾：清掉会话 Cookie，回到纯 RT 场景
            store.set_passport_cookie("")
        finally:
            await engine.stop()

    asyncio.run(run_case())

    # 4.6 过期令牌 + 自动续期后能跑完整执行
    async def run_execute() -> None:
        engine = Engine()
        await engine.start()
        try:
            store.set_refresh_token("RT-ORIGINAL-0123456789abcdef")
            store.set_session_headers(headers_with(fake_jwt(-60)))   # 已过期
            task_id = RESULTS.get("task_id")
            run_id = await engine.trigger(task_id)
            run = store.get_run(run_id)
            check("令牌过期时先自动续期再执行", run["status"] == "success", run["message"])
            check("过期令牌未被直接拦截", "已过期" not in run["message"], run["message"])
        finally:
            await engine.stop()

    asyncio.run(run_execute())

    # 4.7 页面与表单
    from starlette.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        page_html = client.get("/settings").text
        check("设置页有自动续期区块", "令牌自动续期" in page_html)
        check("显示已保存 RT 掩码", "已保存" in page_html)
        check("提供立即续期按钮", 'action="/settings/refresh-now"' in page_html)
        check("接口细节可折叠", "续期接口细节" in page_html and "passport" in page_html)
        check("提供凭据扫描入口", "凭据扫描" in page_html and "/static/sniff.js" in page_html)
        check("扫描脚本可访问", client.get("/static/sniff.js").status_code == 200)

        resp = client.post("/settings/passport-cookie",
                           data={"passport_cookie": "PHPSESSID=ui-test-cookie-9876543210"},
                           follow_redirects=False)
        check("保存会话 Cookie 成功", resp.status_code in (302, 303), str(resp.status_code))
        # 假站点在换令牌时会下发 Set-Cookie，程序应把轮换后的新值存回去
        saved_cookie = store.get_passport_cookie() or ""
        check("会话 Cookie 已加密落盘（取轮换后的新值）",
              saved_cookie.startswith("PHPSESSID=") and "rotated-session" in saved_cookie,
              saved_cookie)

        loc = unquote(resp.headers.get("location", ""))
        check("保存 Cookie 后自动换令牌", ("已换到新令牌" in loc) or ("暂不需要续期" in loc), loc)
        check("保存 Cookie 后自动同步场地", "已同步" in loc, loc)
        check("保存 Cookie 后自动会话自检", "自检" in loc, loc)
        page_html = client.get("/settings").text
        check("设置页不再渲染未掩码变量", "passport_cookie_mask" not in page_html)
        check("设置页显示 Cookie 已保存", "已保存" in page_html)
        before_short = store.get_passport_cookie()
        client.post("/settings/passport-cookie", data={"passport_cookie": "太短"},
                    follow_redirects=False)
        check("过短的 Cookie 不被保存", store.get_passport_cookie() == before_short)
        # 面板整行复制出来的 TSV 形态（Name⇥Value）也要能存
        client.post("/settings/passport-cookie",
                    data={"passport_cookie": "PHPSESSID\ttest-tsv-cookie-abcdefgh"},
                    follow_redirects=False)
        # 归一化是否正确，看实际发给 passport 的 Cookie 头（落库值会被服务端轮换覆盖）
        check("TSV 形态被归一化后发出",
              PASSPORT["last_cookie"] == "PHPSESSID=test-tsv-cookie-abcdefgh",
              PASSPORT["last_cookie"])
        client.post("/settings/passport-cookie", data={"passport_cookie": ""},
                    follow_redirects=False)
        check("清空会话 Cookie 成功", store.get_passport_cookie() == "")

        # 本轮精简：refresh_token 入口整体移除
        settings_page = client.get("/settings").text
        check("设置页已无 refresh_token 输入框", 'name="refresh_token"' not in settings_page)
        check("设置页已无 refresh_token 字样", "refresh_token" not in settings_page)
        resp = client.post("/settings/refresh-token",
                           data={"refresh_token": "RT-NEW-0123456789abcdefghij"},
                           follow_redirects=False)
        check("旧的 refresh-token 路由已下线", resp.status_code == 404, str(resp.status_code))


def main() -> int:
    threading.Thread(target=MOCK.serve_forever, daemon=True).start()
    try:
        test_core()
        test_parse_and_render()
        test_quickstart()
        test_pages()
        test_execution()
        test_token_refresh()
    finally:
        MOCK.shutdown()

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项 / 通过 {PASSED} 项")
        for name in FAILED:
            print("  -", name)
        return 1
    print(f"全部通过：{PASSED} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
