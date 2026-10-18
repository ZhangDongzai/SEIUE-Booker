"""令牌自动续期：用 refresh_token 换新的 access token。

实测事实（2026-10-17，对真实站点验证过接口存在）：

    POST https://api.seiue.com/chalk/oauth/tokens
    Content-Type: application/x-www-form-urlencoded

    grant_type=refresh_token
    refresh_token=<浏览器里那个不透明串>
    client_id=bEGAfhMhb2OZ2yARn8TBxg
    client_secret=8TCt6gW9gwvFnc

要点：
- **必须表单编码**，发 JSON 会被 400 拒绝（服务器要求 grant_type 属性）；
- 该地址由 api.seiue.com 代理到 platform.seiue/oauth/token，无效 RT 会返回
  `{"error":"invalid_request","message":"The refresh token is invalid.", ...}`，
  说明 RT 是服务端加密的不透明串，无法自行构造；
- 成功时返回 `access_token` / `refresh_token` / `expires_in` / `token_type`，
  其中 refresh_token 可能被轮换，必须无条件回写。

本模块只做 HTTP，不读写数据库，方便单独测试。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

from . import config

log = logging.getLogger("booker.oauth")


class RefreshError(RuntimeError):
    """换 token 失败，message 直接可展示给用户。"""

    def __init__(self, message: str, status: int = 0, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


def _error_text(payload: Any, raw: str) -> str:
    """把服务端各种错误形状压成一句人话。

    实测返回过这种双层结构（blink 异常把 OAuth 错误塞进 message 字符串里）：
    {"name":"blink\\core\\HttpException","message":"{\\"error\\":\\"invalid_request\\",...}"}
    """
    if isinstance(payload, dict):
        msg = payload.get("message")
        if isinstance(msg, str) and msg.strip().startswith("{"):
            try:
                inner = json.loads(msg)
            except ValueError:
                inner = None
            if isinstance(inner, dict):
                return _error_text(inner, msg)
        parts = [str(p) for p in (payload.get("error"), msg,
                                  payload.get("error_description"), payload.get("hint")) if p]
        if parts:
            return " / ".join(parts)
        if payload.get("name"):
            return f"{payload['name']}: {msg or ''}"
    text = (raw or "").strip()
    if text.startswith("{"):
        try:
            return _error_text(json.loads(text), text)
        except ValueError:
            pass
    return text[:300]


async def exchange(client: httpx.AsyncClient, refresh_token: str,
                   token_url: str = "") -> dict[str, Any]:
    """用 refresh_token 换一组新令牌；失败抛 RefreshError。"""
    rt = (refresh_token or "").strip()
    if not rt:
        raise RefreshError("没有 refresh_token，无法自动续期")

    url = token_url or config.OAUTH_TOKEN_URL
    form = {
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": config.OAUTH_CLIENT_ID,
        "client_secret": config.OAUTH_CLIENT_SECRET,
    }
    try:
        resp = await client.post(url, data=form, timeout=max(10.0, config.HTTP_TIMEOUT))
    except httpx.HTTPError as exc:
        raise RefreshError(f"续期请求发不出去：{type(exc).__name__}") from exc

    try:
        payload: Any = resp.json()
    except ValueError:
        payload = None

    if resp.status_code >= 400:
        raise RefreshError(
            f"续期被拒（HTTP {resp.status_code}）：{_error_text(payload, resp.text)}",
            status=resp.status_code,
            detail=resp.text[:600],
        )

    data = payload or {}
    # 兼容两种命名：OAuth 标准下划线 / 站点部分接口用驼峰
    access = data.get("access_token") or data.get("accessToken")
    if not access:
        raise RefreshError("续期返回里没有 access_token，响应结构可能已变",
                           status=resp.status_code, detail=resp.text[:600])

    return {
        "access_token": str(access),
        "refresh_token": str(data.get("refresh_token") or data.get("refreshToken") or rt),
        "expires_in": int(data.get("expires_in") or data.get("expiresIn") or 0),
        "token_type": str(data.get("token_type") or data.get("tokenType") or "Bearer"),
        "raw": data,
    }


def needs_refresh(remain_seconds: Optional[int], skew: int = 0) -> bool:
    """剩余有效期不足阈值（默认 600 秒）就该换了；无法判断时保守返回 True。"""
    if remain_seconds is None:
        return True
    return int(remain_seconds) <= (skew or config.TOKEN_REFRESH_SKEW)
