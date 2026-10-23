"""希悦自动订场。

代码地图（建议按这个顺序读）
--------------------------
    config.py     环境变量与常量：端口、时区、站点地址、窗口天数、续期阈值
    store.py      SQLite 访问层：settings / templates / tasks / runs / attempts
    render.py     任务 → 请求：占位符上下文、模板渲染、响应取值
    curlparse.py  「Copy as cURL」→ 结构化请求
    preset.py     预置场地清单模板；把 cURL 转成占位符化模板
    webcore.py    极薄 Web 内核（基于 Starlette）：路由、参数注入、模板、静态文件
    main.py       Web 路由与表单：页面、任务、模板、记录、设置
    runner.py     调度与执行引擎：时钟校准、定时、下单、判定、重试、令牌续期
    tokeninfo.py  解析 JWT 的 exp（只用于展示与提前告警）
    crypto.py     会话凭据加密（标准库 HMAC-CTR + EtM）
    oauth.py      refresh_token 换新令牌
    passport.py   passport 会话 Cookie 免密换新令牌
"""

__version__ = "1.0.1"
