"""默认模板与「一键导入」：把一条真实 cURL 转成可重放的模板。

站点实际形态（2026-10 实测）：
    场地清单  GET  {API}/scms/venue/order-venues
    提交预约  POST {API}/scms/venue/order-venues/{场地ID}/orders
    鉴权      Authorization: Bearer <JWT>（24 小时有效）
"""
from __future__ import annotations

import json
import re
from typing import Any

from . import config, store

VENUE_ID_IN_PATH = re.compile(r"/order-venues/(\d+)/orders")

DEFAULT_LIST = {
    "name": "场地清单（自动同步用）",
    "kind": "list",
    "method": "GET",
    "url": config.VENUES_URL,
    "headers": json.dumps({"accept": "application/json, text/plain, */*"}, ensure_ascii=False),
    "body": "",
    "note": "用于同步场地清单与会话自检；不含时段数据。",
}

def ensure_default_templates() -> None:
    """首次启动只预置「场地清单」模板。

    下单模板不预置：它必须来自你自己那条真实的 POST 请求（「设置 → 一键导入」会自动生成），
    预置一份猜的反而容易在字段或身份头上埋坑。
    """
    if store.list_templates():
        return
    store.save_template(DEFAULT_LIST)


def _replace_venue_id(url: str) -> tuple[str, str]:
    match = VENUE_ID_IN_PATH.search(url)
    if not match:
        return url, ""
    venue_id = match.group(1)
    return VENUE_ID_IN_PATH.sub("/order-venues/{{venue_order_id}}/orders", url), venue_id


def build_order_template(parsed: Any) -> dict:
    """把解析后的 cURL 转成模板字段（占位符化 URL 与请求体）。

    parsed 为 curlparse.ParsedRequest。
    """
    if parsed.method.upper() == "OPTIONS":
        raise ValueError("这是浏览器的预检请求（OPTIONS），请复制真正提交订单的那条 POST 请求")

    url, venue_id = _replace_venue_id(parsed.url)
    warnings: list[str] = []
    if not venue_id:
        warnings.append("URL 中没有识别到场地 ID（/order-venues/数字/orders），已原样保留，请确认是否需要 {{venue_order_id}}")

    headers = {
        key: value for key, value in (parsed.headers or {}).items()
        if key.lower() not in config.SESSION_AUTHORITATIVE
    }
    dropped = len(parsed.headers or {}) - len(headers)
    if dropped:
        warnings.append(f"已把 {dropped} 个身份类请求头（Authorization/Cookie）从模板移出，改由「会话」统一提供")

    body = parsed.body or ""
    template_body = body
    if body.strip().startswith("{"):
        try:
            obj = json.loads(body)
        except ValueError:
            warnings.append("请求体不是合法 JSON，已原样保留，请手动改成占位符形式")
        else:
            if isinstance(obj, dict):
                obj["type"] = "{{order_type}}"
                obj["time_ranges"] = "{{time_ranges}}"
                obj["date_ranges"] = "{{date_ranges}}"
                obj["capacity"] = "{{capacity}}"
                obj["description"] = "{{description}}"
                text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False)
                # 数组/对象需要以裸 JSON 注入，去掉外层引号
                for key in ("time_ranges", "date_ranges"):
                    text = text.replace(f'"{{{{{key}}}}}"', "{{" + key + "}}")
                template_body = text
            else:
                warnings.append("请求体不是 JSON 对象，已原样保留")
    elif body:
        warnings.append("请求体不是 JSON，已原样保留（若含表单字段请手动改成占位符）")

    return {
        "venue_id": venue_id,
        "name": "提交预约",
        "kind": "order",
        "method": parsed.method.upper() or "POST",
        "url": url,
        "headers": json.dumps(headers, ensure_ascii=False, indent=2),
        "body": template_body,
        "note": "\n".join(warnings),
        "warnings": warnings,
    }


def session_headers_from(parsed: Any) -> dict[str, str]:
    """从 cURL 中提取身份与会话相关请求头（含 x-role / x-school-id 这类上下文头）。"""
    keep_prefix = ("x-", "sec-ch-ua")
    keep_exact = {"authorization", "cookie", "user-agent", "accept", "accept-language",
                  "origin", "referer", "content-type"}
    out: dict[str, str] = {}
    for key, value in (parsed.headers or {}).items():
        lower = key.lower()
        if lower in keep_exact or lower.startswith(keep_prefix):
            out[key] = value
    return out
