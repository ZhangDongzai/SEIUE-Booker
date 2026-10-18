"""极薄的 Web 内核：用 Starlette 复刻项目里用到的 FastAPI 写法。

为什么不再依赖 FastAPI
----------------------
FastAPI 的校验层依赖 pydantic v2，而 pydantic-core 是 Rust 扩展。
在 Termux（Android / bionic libc）上，PyPI 的 manylinux wheel 一律不可用，
安装时必须在手机上现场装 Rust 工具链编译，动辄几十分钟且极易失败。

本项目实际只用到 FastAPI 的这几样能力：
路由注册、路径/查询参数注入、模板渲染、静态文件、Response/RedirectResponse、HTTP 中间件。
这些能力全部来自 Starlette（FastAPI 本身就是 Starlette 的一层封装），而 Starlette 是纯 Python。
因此这里用 Starlette 实现一个同名同形的极薄封装，`main.py` 的写法保持不变。
"""
from __future__ import annotations

import inspect
from typing import Any, Callable, Optional

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                                 RedirectResponse, Response)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

__all__ = [
    "App", "Request", "Response", "HTMLResponse", "JSONResponse",
    "PlainTextResponse", "RedirectResponse", "StaticFiles", "Jinja2Templates",
]

_EMPTY = inspect.Parameter.empty

# 注解在 `from __future__ import annotations` 下是字符串，这里做一次映射
_HINTS: dict[str, Any] = {
    "int": int, "float": float, "bool": bool, "str": str,
    "Request": Request, "Optional[int]": int,
}


def _resolve_hint(annotation: Any, default: Any) -> Optional[type]:
    hint = annotation
    if isinstance(hint, str):
        hint = _HINTS.get(hint.strip())
    if hint is None and default is not _EMPTY and default is not None:
        hint = type(default)
    return hint if hint in (int, float, bool, str) else None


def _coerce(raw: str, annotation: Any, default: Any) -> Any:
    """把路径/查询参数里的字符串按注解转成目标类型；转换失败退回默认值。"""
    hint = _resolve_hint(annotation, default)
    try:
        if hint is int:
            return int(raw)
        if hint is float:
            return float(raw)
        if hint is bool:
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
    except (TypeError, ValueError):
        return default if default is not _EMPTY else raw
    return raw


def _as_response(value: Any) -> Response:
    """handler 直接返回 dict/list/str 时自动包装，与原先 FastAPI 的默认行为一致。"""
    if isinstance(value, Response):
        return value
    if isinstance(value, (dict, list)):
        return JSONResponse(value)
    if isinstance(value, str):
        return PlainTextResponse(value)
    if value is None:
        return Response()
    return PlainTextResponse(str(value))


class _Endpoint:
    """把 handler 适配成 Starlette 需要的 `async (request) -> Response`。

    注入规则（与原先 FastAPI 的行为一致）：
      - 形参名为 request 或注解为 Request → 注入 Request 对象
      - 形参名出现在路径参数里 → 取值并按注解转换（如 task_id: int）
      - 形参名出现在查询串里 → 取值并按注解转换
      - 否则用函数默认值；没有默认值则传 None
    同步函数会被丢进线程池执行，避免阻塞事件循环（Starlette 原生行为）。
    """

    def __init__(self, fn: Callable):
        self.fn = fn
        self.is_async = inspect.iscoroutinefunction(fn)
        self.params: list[tuple[str, Any, Any]] = [
            (name, param.annotation, param.default)
            for name, param in inspect.signature(fn).parameters.items()
        ]

    async def handle(self, request: Request) -> Response:
        kwargs: dict[str, Any] = {}
        for name, annotation, default in self.params:
            if name == "request" or _HINTS.get(str(annotation).strip()) is Request:
                kwargs[name] = request
                continue
            if name in request.path_params:
                kwargs[name] = _coerce(request.path_params[name], annotation, default)
                continue
            if name in request.query_params:
                kwargs[name] = _coerce(request.query_params[name], annotation, default)
                continue
            kwargs[name] = None if default is _EMPTY else default

        if self.is_async:
            return _as_response(await self.fn(**kwargs))
        return _as_response(await run_in_threadpool(self.fn, **kwargs))


class App:
    """与 FastAPI 用法一致的极薄封装：只有路由、中间件、挂载与生命周期。"""

    def __init__(self, *, title: str = "", version: str = "", lifespan=None,
                 docs_url: Any = None, redoc_url: Any = None, **_ignored: Any):
        self.title = title
        self.version = version
        self._lifespan = lifespan
        self._routes: list[Any] = []
        self._middleware: list[Callable] = []
        self._app: Optional[Starlette] = None

    # ---------------------------------------------------------- 注册
    def _add(self, path: str, fn: Callable, methods: list[str]) -> Callable:
        # 必须传绑定方法（inspect.ismethod 为真），否则 Starlette 会把对象当成 ASGI app
        self._routes.append(Route(path, _Endpoint(fn).handle, methods=methods))
        self._app = None
        return fn

    def get(self, path: str, **_kwargs: Any) -> Callable[[Callable], Callable]:
        """等价于 FastAPI 的 @app.get。response_class 等参数接受但忽略。"""
        return lambda fn: self._add(path, fn, ["GET"])

    def post(self, path: str, **_kwargs: Any) -> Callable[[Callable], Callable]:
        """等价于 FastAPI 的 @app.post。"""
        return lambda fn: self._add(path, fn, ["POST"])

    def middleware(self, kind: str = "http") -> Callable[[Callable], Callable]:
        """等价于 FastAPI 的 @app.middleware("http")。"""
        def deco(fn: Callable) -> Callable:
            self._middleware.append(fn)
            self._app = None
            return fn
        return deco

    def mount(self, path: str, app: Any, name: str = "") -> None:
        self._routes.append(Mount(path, app=app, name=name))
        self._app = None

    # ------------------------------------------------------------ ASGI
    def build(self) -> Starlette:
        if self._app is None:
            self._app = Starlette(
                routes=self._routes,
                middleware=[Middleware(BaseHTTPMiddleware, dispatch=fn)
                           for fn in self._middleware],
                lifespan=self._lifespan,
            )
        return self._app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self.build()(scope, receive, send)
