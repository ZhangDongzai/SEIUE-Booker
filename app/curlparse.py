"""把浏览器「Copy as cURL (bash)」的内容解析成可重放的请求定义。

只做解析与清洗，不做任何业务假设：URL、头、体原样保留。
"""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Optional

# 由客户端自动生成、重放时不应携带的头
DROP_HEADERS = {
    "host",
    "content-length",
    "connection",
    "accept-encoding",
    "transfer-encoding",
    "keep-alive",
    "proxy-connection",
    "upgrade",
    "http2-settings",
}

VALUE_OPTS = {
    "-X", "--request", "-H", "--header", "-d", "--data", "--data-raw",
    "--data-binary", "--data-ascii", "--data-urlencode", "-b", "--cookie",
    "-A", "--user-agent", "-e", "--referer", "--url", "-o", "--output",
    "-m", "--max-time", "-x", "--proxy", "--connect-timeout", "--retry",
}


@dataclass
class ParsedRequest:
    method: str = "GET"
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "url": self.url,
            "headers": self.headers,
            "body": self.body,
            "warnings": self.warnings,
        }


def _normalize(text: str) -> str:
    t = (text or "").strip()
    if not t:
        raise ValueError("内容为空")
    # Windows / PowerShell 的 ^ 续行
    t = re.sub(r"\^\s*\r?\n", " ", t)
    # bash 的 \ 续行
    t = re.sub(r"\\\s*\r?\n", " ", t)
    # bash ANSI-C 引用 $'...' 退化成普通单引号，避免 shlex 把 $ 当字面量
    t = t.replace("$'", "'")
    return t


def _split_header(raw: str) -> Optional[tuple[str, str]]:
    raw = raw.strip()
    if not raw:
        return None
    # Chrome 有时导出 'x-requested-with;' 这种无值形式
    if ":" not in raw:
        if raw.endswith(";"):
            return raw[:-1].strip(), ""
        return None
    name, _, value = raw.partition(":")
    name = name.strip()
    if not name or name.startswith(":"):  # HTTP/2 伪头
        return None
    return name, value.strip()


def parse_curl(text: str) -> ParsedRequest:
    normalized = _normalize(text)
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError as exc:  # 引号不配对
        raise ValueError(f"cURL 引号不匹配，无法解析：{exc}") from exc
    if not tokens:
        raise ValueError("内容为空")

    # 允许直接粘贴 "curl ..."，也允许不带 curl 前缀
    idx = 0
    if tokens[0].lower() in ("curl", "curl.exe"):
        idx = 1

    req = ParsedRequest()
    body_parts: list[str] = []
    cookie_from_opt: Optional[str] = None
    explicit_method = False

    while idx < len(tokens):
        tok = tokens[idx]

        if tok == "--url" and idx + 1 < len(tokens):
            req.url = req.url or tokens[idx + 1]
            idx += 2
            continue

        # -XPOST 这类合并写法
        if tok.startswith("-X") and len(tok) > 2 and not tok.startswith("--"):
            req.method = tok[2:].upper()
            explicit_method = True
            idx += 1
            continue

        if tok in ("-X", "--request") and idx + 1 < len(tokens):
            req.method = tokens[idx + 1].upper()
            explicit_method = True
            idx += 2
            continue

        if tok in ("-H", "--header") and idx + 1 < len(tokens):
            pair = _split_header(tokens[idx + 1])
            if pair:
                req.headers[pair[0]] = pair[1]
            idx += 2
            continue

        if tok in ("-b", "--cookie") and idx + 1 < len(tokens):
            cookie_from_opt = tokens[idx + 1]
            idx += 2
            continue

        if tok in ("-A", "--user-agent") and idx + 1 < len(tokens):
            req.headers["User-Agent"] = tokens[idx + 1]
            idx += 2
            continue

        if tok in ("-e", "--referer") and idx + 1 < len(tokens):
            req.headers["Referer"] = tokens[idx + 1]
            idx += 2
            continue

        if tok in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii") and idx + 1 < len(tokens):
            body_parts.append(tokens[idx + 1])
            idx += 2
            continue

        if tok == "--data-urlencode" and idx + 1 < len(tokens):
            body_parts.append(tokens[idx + 1])
            idx += 2
            continue

        if tok in VALUE_OPTS and idx + 1 < len(tokens):
            idx += 2
            continue

        if tok.startswith("-"):
            # 未知开关（--compressed / -k / -s / -L / --http2 ...），忽略
            idx += 1
            continue

        if not req.url:
            req.url = tok
            idx += 1
            continue

        idx += 1

    if not req.url:
        raise ValueError("未找到 URL，请确认复制的是「Copy as cURL (bash)」")
    if not req.url.lower().startswith(("http://", "https://")):
        raise ValueError(f"URL 不合法：{req.url}")

    # 清洗头
    cleaned: dict[str, str] = {}
    for name, value in req.headers.items():
        if name.lower() in DROP_HEADERS:
            continue
        cleaned[name] = value

    if cookie_from_opt:
        if "Cookie" in cleaned:
            cleaned["Cookie"] = f"{cleaned['Cookie']}; {cookie_from_opt}".strip("; ")
        else:
            cleaned["Cookie"] = cookie_from_opt

    req.headers = cleaned
    req.body = "".join(body_parts)

    if req.body and not explicit_method:
        req.method = "POST"
    if not req.method:
        req.method = "GET"

    if not any(k.lower() == "cookie" for k in req.headers):
        req.warnings.append("请求头中未发现 Cookie，若接口需要登录态请在「设置」中单独导入会话。")
    if not any(k.lower() == "content-type" for k in req.headers) and req.body:
        req.warnings.append("请求体非空但没有 Content-Type，重放可能被服务端拒绝。")

    return req


def parse_headers_input(text: str) -> dict[str, str]:
    """设置页用：既可粘贴 cURL，也可粘贴纯 `Key: Value` 行。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("内容为空")
    if raw.lower().startswith("curl"):
        return parse_curl(raw).headers

    headers: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip().rstrip("\\").strip()
        if not line:
            continue
        pair = _split_header(line)
        if pair:
            headers[pair[0]] = pair[1]
    if not headers:
        raise ValueError("未识别到任何请求头，请粘贴 cURL 或 `Key: Value` 形式的文本")
    return headers


def pretty_body(body: str) -> str:
    """尽量把 JSON 体缩进显示，失败则原样返回。"""
    if not body:
        return ""
    stripped = body.strip()
    if not stripped.startswith(("{", "[")):
        return body
    try:
        return json.dumps(json.loads(stripped), ensure_ascii=False, indent=2)
    except ValueError:
        return body
