"""从会话头里解析 Bearer JWT 的有效期。

只做 base64 解码用于展示与提前告警，**不做签名校验**（也不需要）。
站点签发的是 RS256 JWT，实测有效期 24 小时，过期后必须重新导入。
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any, Optional


def _b64url(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def find_bearer(headers: dict[str, str]) -> str:
    for key, value in (headers or {}).items():
        if key.lower() != "authorization":
            continue
        raw = str(value or "").strip()
        if raw.lower().startswith("bearer "):
            return raw[7:].strip()
    return ""


def decode(token: str) -> Optional[dict[str, Any]]:
    """解出 JWT 的 payload；结构不符返回 None。"""
    parts = (token or "").split(".")
    if len(parts) < 2:
        return None
    try:
        payload = json.loads(_b64url(parts[1]))
    except (ValueError, TypeError, base64.binascii.Error):
        return None
    return payload if isinstance(payload, dict) else None


def inspect(headers: dict[str, str], now: Optional[float] = None) -> dict[str, Any]:
    """返回可直接给页面用的令牌状态。"""
    token = find_bearer(headers)
    if not token:
        return {"present": False, "kind": "cookie" if headers else "none"}
    payload = decode(token)
    if payload is None:
        return {"present": True, "kind": "unknown", "valid": False}

    now = time.time() if now is None else now
    exp = payload.get("exp")
    iat = payload.get("iat")
    sub = payload.get("sub")
    out: dict[str, Any] = {
        "present": True,
        "kind": "bearer",
        "sub": sub,
        "iat": iat,
        "exp": exp,
        "school": payload.get("aud"),
        "sid": payload.get("sid"),
    }
    if not isinstance(exp, (int, float)):
        out["valid"] = True
        return out

    remain = float(exp) - now
    out["remain_seconds"] = int(remain)
    out["expired"] = remain <= 0
    out["valid"] = remain > 0
    out["warn"] = 0 < remain < 3 * 3600
    if isinstance(iat, (int, float)):
        out["lifetime_hours"] = round((float(exp) - float(iat)) / 3600, 1)
    return out


def describe(info: dict[str, Any]) -> str:
    """给页面的一句话说明。"""
    if not info.get("present"):
        if info.get("kind") == "cookie":
            return "已导入 Cookie 会话（未检测到 Bearer 令牌）"
        return "尚未导入会话"
    if info.get("kind") == "unknown":
        return "Authorization 头格式无法识别（应形如 Bearer eyJ...）"
    if info.get("exp") is None:
        return "令牌无 exp 字段，无法判断有效期"
    if info.get("expired"):
        return "令牌已过期，必须重新导入才可以预约"
    remain = int(info.get("remain_seconds") or 0)
    hours, minutes = divmod(remain // 60, 60)
    return f"令牌剩余有效 {hours} 小时 {minutes} 分"
