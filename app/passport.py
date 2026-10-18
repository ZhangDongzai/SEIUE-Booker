"""免密换新令牌：用 passport 域的会话 cookie 直接签发新的 access token。

实测依据（2026-10-17，扒前端 bundle `72841.835e460b34.js` 得到前端自己的实现）：

    // 有 refreshToken 时走 api 域：
    api.post("/chalk/oauth/tokens", {grantType:"refresh_token", clientId, clientSecret, refreshToken})
    // 没有 refreshToken 时（Web 端实际走的就是这条）：
    axios.post(PASSPORT + "/authorize",
        qs.stringify({response_type:"token", client_id: CLIENT_WEB_CLIENT_ID}),
        {withCredentials: true})

    SERVER_PASSPORT      = https://passport.seiue.com
    CLIENT_WEB_CLIENT_ID = GpxvnjhVKt56qTmnPWH1sA
    CLIENT_APP_CLIENT_ID = bEGAfhMhb2OZ2yARn8TBxg

本站点 Web 端**不保存 refresh_token**（localStorage 的 persist:root 里只有
accessToken / tokenType / expiresIn），所以上面第二条就是它唯一的续期通道：
依赖 `passport.seiue.com` 域下那个 HttpOnly 会话 cookie。

服务端直连实测（无 cookie）：

    无 cookie  POST /authorize  →  401 {"name":"Unauthorized","message":"Login required"}
    响应头带 Access-Control-Allow-Credentials: true（前端因此能跨域带 cookie 调它）
    服务端 PHP/7.4.3 + blink，会话 cookie 形如 PHPSESSID=...; HttpOnly; SameSite=Lax

浏览器侧实测（2026-10-17 21:00，sniff.js v3 于已登录页面执行）：

    POST passport/authorize（credentials: include）→ HTTP 201
    {"token_type":"bearer","expires_in":"86400","access_token":"eyJhbGciOiJSUzI1NiJ9..."}

即通道已验证可用：浏览器带着 passport 域的 HttpOnly 会话 cookie 就能免密签到新令牌，
且 expires_in 是**字符串** "86400"（解析时要兼容）。

因此只要把那个 cookie 存进程序，就能在令牌快过期时免密换新的——
**不需要账号密码，不需要 refresh_token**。

本模块只做 HTTP，不读写数据库，方便单独测试。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from . import config

log = logging.getLogger("booker.passport")

# 从响应体/URL 里认 access_token 的几种形态
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}")


class PassportError(RuntimeError):
    """换 token 失败，message 直接可展示给用户。"""

    def __init__(self, message: str, status: int = 0, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


# Set-Cookie 里的属性段不是 cookie 本身，拼 Cookie 头时要剔掉
_COOKIE_ATTRS = {"path", "domain", "expires", "max-age", "httponly",
                 "secure", "samesite", "comment", "version", "priority"}


def _normalize_cookie(raw: str) -> str:
    """把用户可能粘贴的各种形态统一成 Cookie 头值。

    接受：
      PHPSESSID=abc123
      PHPSESSID=abc123; other=xyz
      cookie: PHPSESSID=abc123        （整行带头名）
      PHPSESSID<TAB>abc123            （DevTools 面板整行复制出来的 TSV）
      PHPSESSID=abc123\nother=xyz     （多条 cookie 各占一行）
      PHPSESSID=abc; Path=/; HttpOnly （整条 Set-Cookie，属性段自动剔除）
    """
    text = (raw or "").strip()
    if not text:
        return ""
    pairs: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        for prefix in ("cookie:", "set-cookie:"):
            if low.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        if "=" in line:
            for seg in line.split(";"):
                seg = seg.strip()
                if not seg or "=" not in seg:
                    continue
                name = seg.split("=", 1)[0].strip().lower()
                if name in _COOKIE_ATTRS:
                    continue
                pairs.append(re.sub(r"\s+", " ", seg).strip())
        else:
            # 没有等号：多半是面板复制的「Name<空白>Value」
            parts = line.split()
            if len(parts) >= 2:
                pairs.append(parts[0] + "=" + parts[1])
    return "; ".join(p for p in pairs if p)


def normalize_cookie(raw: str) -> str:
    """_normalize_cookie 的公开别名（保存前先归一化再校验）。"""
    return _normalize_cookie(raw)


def _merge_cookie(old_header: str, set_cookies: list[str]) -> str:
    """把响应里的 Set-Cookie 合并进现有 Cookie 头。

    同名替换、异名追加、空值视为删除。服务端轮换会话 cookie 时，
    程序靠这个把新值存回去，会话才能一直滚动下去。
    """
    pairs: list[list[str]] = []
    for item in (old_header or "").split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, _, value = item.partition("=")
        name, value = name.strip(), value.strip()
        if name:
            pairs.append([name, value])
    for sc in set_cookies or []:
        first = (sc or "").split(";", 1)[0].strip()
        if "=" not in first:
            continue
        name, _, value = first.partition("=")
        name, value = name.strip(), value.strip()
        if not name or name.lower() in _COOKIE_ATTRS:
            continue
        for exist in pairs:
            if exist[0].lower() == name.lower():
                exist[1] = value
                break
        else:
            if value:
                pairs.append([name, value])
    return "; ".join(f"{n}={v}" for n, v in pairs if v)


def _extract(payload: Any, raw: str, status: int) -> str:
    """从 JSON / 纯文本 / 重定向地址里找出 access_token。"""
    if isinstance(payload, dict):
        for key in ("access_token", "accessToken", "token", "access"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        # 有些实现套一层 data
        inner = payload.get("data")
        if isinstance(inner, dict):
            return _extract(inner, raw, status)
    m = _JWT_RE.search(raw or "")
    if m:
        return m.group(0)
    return ""


async def mint(client: httpx.AsyncClient, cookie: str,
               client_id: str = "", url: str = "") -> dict[str, Any]:
    """用 passport 会话 cookie 换一个新 access token；失败抛 PassportError。

    返回的 set_cookie 是服务端轮换后的新 Cookie 头值（没轮换则为空串），
    调用方应回写掉旧值，否则下次会话可能失效。
    """
    cookie_header = _normalize_cookie(cookie)
    if not cookie_header:
        raise PassportError("没有会话 cookie，无法免密换令牌")

    cid = client_id or config.PASSPORT_WEB_CLIENT_ID
    target = url or config.PASSPORT_AUTHORIZE_URL

    # 带上浏览器会发的头，避免被「只认浏览器」的规则挡下
    extra = {"Cookie": cookie_header,
             "Content-Type": "application/x-www-form-urlencoded",
             "Accept": "application/json, text/plain, */*",
             "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/131.0.0.0 Safari/537.36"}
    for key, value in (("Origin", config.SITE), ("Referer", config.SITE + "/")):
        if value and not value.startswith("http://127.0.0.1"):
            extra[key] = value

    try:
        resp = await client.post(
            target,
            data={"response_type": "token", "client_id": cid},
            headers=extra,
            timeout=max(10.0, config.HTTP_TIMEOUT),
        )
    except httpx.HTTPError as exc:
        raise PassportError(f"请求发不出去：{type(exc).__name__}") from exc

    raw = resp.text or ""
    try:
        payload: Any = resp.json()
    except ValueError:
        payload = None

    if resp.status_code in (401, 403):
        raise PassportError(
            "会话 cookie 已失效（HTTP %d），请重新登录后从 F12 → Application → "
            "Cookies → https://passport.seiue.com 复制新的值" % resp.status_code,
            status=resp.status_code, detail=raw[:600])
    if not (200 <= resp.status_code < 300):
        raise PassportError(
            f"免密换令牌被拒（HTTP {resp.status_code}）：{(raw or '')[:200]}",
            status=resp.status_code, detail=raw[:600])

    token = _extract(payload, raw, resp.status_code)
    if not token:
        raise PassportError(
            "响应里没有 access_token，站点结构可能已变",
            status=resp.status_code, detail=raw[:600])

    expires_in = 0
    if isinstance(payload, dict):
        for key in ("expires_in", "expiresIn"):
            value = payload.get(key)
            # 站点实测把 expires_in 当成字符串返回（"86400"），两种都要认
            if isinstance(value, str) and value.strip().isdigit():
                expires_in = int(value.strip())
                break
            if isinstance(value, (int, float)) and value > 0:
                expires_in = int(value)
                break

    # 服务端可能借这次响应轮换会话 cookie，带回新值供调用方落盘
    set_cookies = resp.headers.get_list("Set-Cookie")
    rotated = _merge_cookie(cookie_header, set_cookies)
    return {
        "access_token": token,
        "expires_in": expires_in,
        "set_cookie": rotated if rotated != cookie_header else "",
        "raw": payload if isinstance(payload, dict) else {"text": raw[:600]},
    }
