# 希悦自动订场

针对校园场馆系统（`chalk-c3.seiue.com`）的自托管预约助手：网页界面配置场地与时段，在可预约窗口内自动提交请求，并留下完整请求/响应痕迹。

- **接口已实测**：`POST https://api.seiue.com/scms/venue/order-venues/{场地ID}/orders`，Bearer 鉴权，窗口 7 天。见 [05](docs/05-实测接口与联调.md)。
- **一条 cURL 完成配置**：粘贴一次真实下单请求，自动拆出会话令牌、把 URL 与请求体换成占位符。
- **目标日期 + 窗口内立即执行**：选一个具体日期，能约就马上提交；还没进窗口就等到窗口开启那一刻。
- **单进程自托管**：`python run.py`，8 个纯 Python 包，无数据库、无编译器、无容器。
- **两种运行方式**：Termux 手动跑，或装 KernelSU 模块（开机自启 + 网页控制台）。
- **令牌自动续期**：配一次 `passport` 会话 Cookie，之后免密换新。见 [06](docs/06-令牌自动续期.md)。

## 快速开始

### 方式一：Termux / 电脑

```bash
cd badminton-booker
python -m pip install -r requirements.txt
python run.py
```

浏览器打开 `http://127.0.0.1:8080`。手机上的完整步骤见 [07 Termux 部署](docs/07-Termux%20部署.md)。

### 方式二：KernelSU 模块（开机自启）

1. KernelSU 管理器 → 模块 → 从本地安装 `badminton-booker-ksu-v1.0.0.zip` → 重启；
2. 打开该模块的网页控制台，点「安装依赖」；
3. 点「启动」，再点「打开完整界面」。

详见 [08 KernelSU 模块](docs/08-KernelSU%20模块.md)。

### 首次配置

设置 →「一键导入」粘贴下单那条 POST 的 cURL → 配置会话 Cookie（会自动同步场地并自检）→ 建任务（先开「演练模式」跑一次）。

> ⚠️ 站点令牌 **24 小时过期**。配了会话 Cookie 就自动续期。
> 本站点 Web 端不保存 refresh_token，用 `passport.seiue.com` 域的会话 Cookie 才是对的。

## 文档

| 文档 | 内容 |
| --- | --- |
| [01 需求说明](docs/01-需求说明.md) | 功能清单、模板变量表、已确认事实、验收标准 |
| [02 架构设计](docs/02-架构设计.md) | 模块划分、数据模型、执行时序、精度策略、风险 |
| [03 依赖与部署](docs/03-依赖与部署.md) | 依赖清单、前台/服务化部署（systemd）、运维与排障 |
| [04 抓包与配置](docs/04-抓包与模板配置指南.md) | 怎么复制那一条 cURL、怎么核对 |
| [05 实测接口](docs/05-实测接口与联调.md) | 实测的接口/字段/状态码、审批流、每日运维 |
| [06 令牌自动续期](docs/06-令牌自动续期.md) | 续期通道（passport 会话 Cookie）、校验与排错 |
| [07 Termux 部署](docs/07-Termux%20部署.md) | 手机运行：环境、安装、启动、访问、保活、验证 |
| [08 KernelSU 模块](docs/08-KernelSU%20模块.md) | 模块安装、开机自启、网页控制台、配置与排查 |

## 目录结构

```
run.py             启动器（打印访问地址、端口检测）
termux-setup.sh    Termux 一键安装：装依赖 + 自检
kernelsu-module/   KernelSU 模块源码（开机自启 + 网页控制台）
app/
├── main.py        Web 路由、表单、鉴权
├── webcore.py     极薄 Web 内核（基于 Starlette）
├── config.py      环境变量与路径
├── crypto.py      会话加密（标准库实现）
├── curlparse.py   cURL → 请求定义
├── tokeninfo.py   JWT 有效期解析
├── preset.py      默认模板 / cURL 一键转模板
├── store.py       SQLite 访问层（含自动补列升级）
├── render.py      占位符渲染 / 字段提取 / 候选生成
├── runner.py      时钟校准、任务协程、执行编排
├── views/         Jinja2 页面
└── static/        样式与脚本（无 CDN 依赖）
data/              运行时生成：app.db、secret.key（需备份）
docs/              项目文档
tests/             端到端冒烟测试（本地假站点）
```

## 核心概念

- **会话**：从 cURL 里提取的 `Authorization: Bearer …` 与 `x-role`/`x-school-id` 等上下文头，加密落盘；身份只此一处，模板不存。
- **下单模板**：`POST …/order-venues/{{venue_order_id}}/orders`，请求体 5 个字段用占位符注入。
- **任务**：场地白名单（勾选顺序即尝试顺序）+ 目标日期 + 时段；窗口内立即执行，窗口外等到开启时刻。
- **演练模式**：只列将尝试的场地、不提交，用来验证配置。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `APP_HOST` | 0.0.0.0 | 监听地址。Termux 上想只让本机访问就设 `127.0.0.1` |
| `APP_PORT` | 8080 | 监听端口 |
| `APP_PASSWORD` | 空 | 访问口令，留空则免登录 |
| `APP_TZ` | Asia/Shanghai | 时区 |
| `DATA_DIR` | 项目内 `data/` | 数据目录（`app.db` 与 `secret.key`） |
| `APP_PROXY` | 空 | 默认直连；必须过代理时才填 |
| `HTTP_TIMEOUT` | 10 | 单请求超时（秒） |
| `TOKEN_REFRESH_SKEW` | 600 | 令牌剩余不足多少秒就自动续期 |
| `TOKEN_CHECK_SECONDS` | 300 | 后台巡检令牌的间隔（秒） |
| `OAUTH_CLIENT_ID` | 内置 | 换 token 用的公共 client；学校轮换时覆盖 |
| `OAUTH_CLIENT_SECRET` | 内置 | 同上 |
| `LOG_LEVEL` | INFO | 日志级别 |

## 本地开发与测试

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Windows
DATA_DIR=./data .venv/Scripts/python -m uvicorn app.main:app --reload --port 8080
.venv/Scripts/python tests/smoke_test.py           # 73 项端到端检查（本地假站点）
```

## 注意事项

- 预约窗口为未来 7 天，抢窗口最后一天请把「日期偏移」设为 7。
- 6 块羽毛球场地均为 `need_approval = true`：程序提交成功后，**仍需你到「我的预约」审批**。
- 场地开放时间实测为工作日 `16:35–18:30`，超出时段的请求大概率被拒。
- NAS 需关闭休眠，否则可能错过放号时刻；建议接入 UPS。
- 本工具仅用于本人账号的本人预约，不含验证码破解、多账号代抢、代理池或高频轰炸。请遵守学校场馆管理规定。
