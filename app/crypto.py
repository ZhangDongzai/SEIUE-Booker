"""会话凭据的加密存储（只依赖标准库，Termux 上零编译）。

构造
----
主密钥 32 字节随机数，存 `data/secret.key`（urlsafe-base64，权限 0600）。
每次加密随机生成 16 字节 IV，用 HMAC-SHA256 派生密钥流做 CTR 式异或，
再对整个密文做 HMAC-SHA256 认证（Encrypt-then-MAC）：

    密文 = "b1." + base64url( IV ‖ C ‖ TAG[:16] )

这不是自创密码学——HMAC 作为 PRF 派生密钥流、再做 EtM 认证是标准构造，
安全性等同 AES-CTR + HMAC，且只用 `hashlib` / `hmac` / `os.urandom`。
在 Termux、Alpine、Windows 上行为完全一致，不需要编译任何东西。

兼容性
------
`secret.key` 的格式与旧版 Fernet 密钥完全相同（都是 32 字节的 urlsafe-base64），
所以升级到这个版本不需要迁移密钥。密文带 `b1.` 前缀；
读取时若遇到旧版 Fernet 密文（`gAAAAA` 开头）、且环境里恰好装了 cryptography，
会自动用它解密，避免老数据丢失。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any

from . import config

PREFIX = "b1."
_IV_LEN = 16
_TAG_LEN = 16


# ------------------------------------------------------------------ 密钥

def _load_key() -> bytes:
    """读取或生成主密钥，返回 urlsafe-base64 编码的字节串。"""
    config.ensure_dirs()
    path = config.KEY_PATH
    if os.path.exists(path):
        with open(path, "rb") as fh:
            key = fh.read().strip()
        if key:
            return key
    key = base64.urlsafe_b64encode(os.urandom(32))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def _master() -> bytes:
    raw = _load_key()
    try:
        data = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
        if len(data) >= 32:
            return data[:32]
    except (ValueError, TypeError):
        pass
    return hashlib.sha256(raw).digest()


def _subkeys() -> tuple[bytes, bytes]:
    master = _master()
    k_enc = hmac.new(master, b"booker-enc-v1", hashlib.sha256).digest()
    k_mac = hmac.new(master, b"booker-mac-v1", hashlib.sha256).digest()
    return k_enc, k_mac


# ------------------------------------------------------------- 对称加密

def _keystream(key: bytes, iv: bytes, size: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hmac.new(key, iv + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:size])


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _legacy_decrypt(token: str) -> str | None:
    """旧版 Fernet 密文的兼容读取；环境里没有 cryptography 就放弃。"""
    try:
        from cryptography.fernet import Fernet
    except Exception:
        return None
    try:
        return Fernet(_load_key()).decrypt(token.encode("ascii")).decode("utf-8")
    except Exception:
        # 解不开就当没有（旧版数据、或 key 不匹配）
        return None


# ------------------------------------------------------------------ 对外

def app_secret() -> str:
    """登录 Cookie 的签名密钥，与加密密钥同源，避免额外配置。"""
    return _load_key().decode("ascii", "replace")


def encrypt_text(text: str) -> str:
    """加密单个字符串（会话头、refresh_token、passport cookie 都用它）。"""
    if not text:
        return ""
    k_enc, k_mac = _subkeys()
    iv = os.urandom(_IV_LEN)
    raw = text.encode("utf-8")
    body = _xor(raw, _keystream(k_enc, iv, len(raw)))
    tag = hmac.new(k_mac, PREFIX.encode() + iv + body, hashlib.sha256).digest()[:_TAG_LEN]
    return PREFIX + _b64e(iv + body + tag)


def decrypt_text(token: str, default: str = "") -> str:
    if not token:
        return default
    if not token.startswith(PREFIX):
        legacy = _legacy_decrypt(token)
        return legacy if legacy is not None else default
    try:
        blob = _b64d(token[len(PREFIX):])
        if len(blob) < _IV_LEN + _TAG_LEN:
            return default
        iv = blob[:_IV_LEN]
        body = blob[_IV_LEN:-_TAG_LEN]
        tag = blob[-_TAG_LEN:]
        k_enc, k_mac = _subkeys()
        want = hmac.new(k_mac, PREFIX.encode() + iv + body, hashlib.sha256).digest()[:_TAG_LEN]
        if not hmac.compare_digest(tag, want):
            return default
        return _xor(body, _keystream(k_enc, iv, len(body))).decode("utf-8")
    except (ValueError, TypeError, UnicodeDecodeError):
        return default


def encrypt_json(obj: Any) -> str:
    return encrypt_text(json.dumps(obj, ensure_ascii=False))


def decrypt_json(token: str, default: Any = None) -> Any:
    text = decrypt_text(token, "")
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


SENSITIVE = ("cookie", "authorization", "x-csrf-token", "token", "x-xsrf-token")


def mask_value(value: str, keep: int = 6) -> str:
    value = value or ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}…{value[-keep:]}"


def mask_headers(headers: dict[str, str]) -> dict[str, str]:
    out = {}
    for k, v in (headers or {}).items():
        out[k] = mask_value(v) if k.lower() in SENSITIVE else v
    return out
