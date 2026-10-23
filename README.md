# 希悦自动订场

针对校园场馆系统（`chalk-c3.seiue.com`）的自托管预约助手：在网页里配置日期、时段、场地，
程序在可预约窗口开启时自动提交，并把每一次请求与响应完整留痕。

- **单进程、纯 Python**：8 个依赖全是 `py3-none-any`，不需要数据库服务、不需要编译器、不需要容器。
- **一条 cURL 完成接入**：粘贴一次真实下单请求，自动拆出令牌、把 URL 与请求体换成占位符。
- **目标日期驱动**：选一个具体日期，在窗口内就立刻执行，没进窗口就等到窗口开启那一刻。
- **令牌自动续期**：填一次 `passport` 域会话 Cookie，之后快过期时免密换新。
- **两种跑法**：Termux / 电脑直接 `python run.py`；或装 KernelSU 模块，开机自启 + 模块页内嵌控制台。

## 快速开始

```bash
python -m pip install -r requirements.txt
python run.py
```

浏览器打开 `http://127.0.0.1:8080`，然后：

1. **设置 → 一键导入**：粘贴「提交预约」那条 POST 的 cURL（浏览器 F12 → 网络 → 右键 Copy as cURL）
2. **设置 → 会话 Cookie**：粘贴 `passport.seiue.com` 域的会话 Cookie，保存后会自动换令牌、同步场地、自检
3. **任务 → 新建**：选预约日期与时段、勾选场地、填人数；先开「演练模式」跑一次确认无误
4. 取消演练、保存启用

## 打包 KernelSU 模块

```bash
python build-module.py
# → dist/badminton-booker-ksu-v1.0.1.zip
```

刷入：KernelSU 管理器 → 模块 → 从本地安装 → 重启。详见 [06 模块与打包](docs/06-模块与打包.md)。

## 目录结构

```
badminton-booker/
├── app/                   程序本体
│   ├── main.py            Web 路由与表单
│   ├── webcore.py         极薄 Web 内核（基于 Starlette）
│   ├── runner.py          调度与执行引擎
│   ├── render.py          任务 → 请求（占位符）
│   ├── store.py           SQLite 访问层
│   ├── config.py          环境变量与常量
│   ├── preset.py          预置模板 / cURL 转模板
│   ├── curlparse.py       cURL 解析
│   ├── tokeninfo.py       JWT 有效期解析
│   ├── crypto.py          凭据加密
│   ├── oauth.py           refresh_token 续期
│   ├── passport.py        会话 Cookie 免密续期
│   ├── views/             页面模板
│   └── static/            样式与脚本
├── kernelsu-module/       KernelSU 模块源码
│   ├── module.prop        模块信息（名称/版本/运行状态）
│   ├── service.sh         开机自启 + 状态心跳 + 看门狗
│   ├── ctl.sh             启停/状态/诊断/自检
│   ├── post-fs-data.sh    早期初始化
│   ├── customize.sh       安装时权限处理
│   ├── uninstall.sh       卸载清理
│   └── webroot/index.html 模块网页控制台
├── docs/                  文档（按架构分层组织）
├── tests/                 冒烟测试（208 项 + WebUI 60 项）
├── build-module.py        打包脚本
├── run.py                 本地启动器
├── requirements.txt       运行时依赖
└── .env.example           环境变量示例
```

## 文档

| 文档 | 内容 |
| --- | --- |
| [01 架构总览](docs/01-架构总览.md) | 分层图、两条主线、模块职责、设计取舍、代码地图 |
| [02 运行与部署](docs/02-运行与部署.md) | 依赖安装、启动参数、常驻、环境变量、数据与备份 |
| [03 使用指南](docs/03-使用指南.md) | 首次配置、页面地图、任务字段、模板与记录 |
| [04 调度与执行](docs/04-调度与执行.md) | 时钟校准、执行时机决策、下单流程、令牌续期 |
| [05 数据与接口](docs/05-数据与接口.md) | 表结构、占位符全集、站点接口实测 |
| [06 模块与打包](docs/06-模块与打包.md) | KernelSU 模块结构、WebUI 契约、打包与排错 |

## 环境变量

全部可选，写进 `.env` 或直接用环境变量传入。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `APP_HOST` | `0.0.0.0` | 监听地址，`127.0.0.1` 表示只允许本机 |
| `APP_PORT` | `8080` | 监听端口 |
| `DATA_DIR` | 项目内 `data/` | 数据目录（`app.db` + `secret.key`） |
| `APP_PASSWORD` | 空 | 访问口令，留空则不鉴权 |
| `APP_TZ` | `Asia/Shanghai` | 时区 |
| `APP_PROXY` | 空 | 出网代理；默认直连，不读系统 `HTTP(S)_PROXY` |
| `MAX_ORDER_DAYS` | `7` | 站点可预约窗口天数 |
| `RETRY_SECONDS` | `600` | 窗口内未成功后的重试间隔 |
| `TOKEN_REFRESH_SKEW` | `600` | 令牌剩余不足多少秒就续期 |
| `TOKEN_CHECK_SECONDS` | `300` | 后台令牌巡检间隔 |
| `HTTP_TIMEOUT` | `10` | 单次请求超时（秒） |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## 边界说明

- 站点令牌 **24 小时过期**。不配会话 Cookie 也能用，但要每天重贴一次 cURL；配了则自动续期。
- 预约提交后状态是 **`initiated`（待审批）**，需要本人到「我的预约」确认，程序不代审批。
- 站点没有「时段余量」接口，所以程序按配置的场地顺序直接提交，失败自动换下一块。
- 仅供个人预约使用；请遵守学校场馆管理规定。

## 测试

```bash
python tests/smoke_test.py          # 208 项：解析/渲染/存储/HTTP/页面/执行
node tests/webui_exec_test.js       # 60 项：模块控制台的 KernelSU 桥接兼容性
```
