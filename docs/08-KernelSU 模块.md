# 08 KernelSU 模块

第二种运行方式：把程序做成 KernelSU 模块，**开机自动启动**，并能在 KernelSU 管理器的模块页面里直接控制。

与 Termux 方式的关系：程序逻辑完全一致，只是启动方式与访问入口不同。

---

## 1. 前提

| 项目 | 要求 |
| --- | --- |
| 系统 | Android 16 / HyperOS 3（已解 BL），已安装 KernelSU 或 KernelSU Next |
| Termux | 已安装，且执行过 `pkg install python`（模块复用它的 Python 运行时） |
| 模块包 | `badminton-booker-ksu-v1.0.0.zip` |

> 模块本身不带 Python 解释器。Android 没有系统级 Python，复用 Termux 的运行时是唯一务实的选择。

---

## 2. 安装

1. 把 `badminton-booker-ksu-v1.0.0.zip` 放到手机里；
2. KernelSU 管理器 → **模块** → **从本地安装** → 选这个 zip；
3. 安装完成后**重启手机**（`service.sh` 依赖完整的开机流程）；
4. 回到模块列表，找到「希悦自动订场」，点进去打开**网页控制台**。

控制台长这样：

```
● 已停止
  端口 8080    PID —
  健康检查 —   Python 3.12.x
  数据目录 /data/adb/badminton-booker/data

[ 打开完整界面 ]
[ 启动 ] [ 停止 ] [ 重启 ]
[ 安装依赖 ] [ 刷新日志 ] [ 刷新状态 ]
```

---

## 3. 安装依赖（首次必做）

模块里的程序需要 8 个纯 Python 包。两种方式任选：

**A. 在控制台点「安装依赖」**（推荐，会在后台执行 `pip install`，约 1 分钟）

**B. 在 Termux 里手动装**：

```bash
pkg install python
pip install starlette uvicorn jinja2 python-multipart httpx jmespath itsdangerous tzdata
```

装完点「刷新状态」，Python 后面显示版本号、健康检查不再是 `—` 就对了。

---

## 4. 使用

| 操作 | 结果 |
| --- | --- |
| 点「启动」 | 后台拉起服务，约 3–5 秒后状态变成 `● 运行中` |
| 点「在本页打开界面」 | **直接在模块页内嵌**加载 `http://127.0.0.1:8080`，不再跳浏览器 |
| 内嵌加载受阻 | 点「用浏览器打开」交回系统浏览器 |
| 点「停止」/「重启」 | 结束进程 / 重启进程 |
| 点「刷新日志」 | 显示最近 80 行运行日志 |
| 点「环境自检」 | 列出命令通道、身份、模块目录、ctl.sh 与 webroot 实际情况（毫秒级） |
| 点「完整诊断」 | 在上面基础上再加 Python 版本、依赖、健康检查与最近日志 |
| 重启手机 | 自动启动，无需手动操作 |

首次进入完整界面后，配置流程与 [03 文档第 5 节](03-依赖与部署.md) 完全一致：
一键导入 cURL → 同步场地（配置会话 Cookie 后会自动完成）→ 建任务。

---

## 5. 目录与配置

| 路径 | 内容 |
| --- | --- |
| `/data/adb/modules/badminton_booker/` | 模块本体（含程序代码 `app/`） |
| `/data/adb/badminton-booker/data/` | **数据目录**：`app.db` + `secret.key` |
| `/data/adb/badminton-booker/booker.log` | 程序运行日志 |
| `/data/adb/badminton-booker/service.log` | 开机启动日志 |
| `/data/adb/badminton-booker/booker.pid` | 进程号 |
| `/data/adb/badminton-booker/heartbeat.pid` | 状态心跳进程号（每 20 秒刷新一次快照） |
| `/data/adb/badminton-booker/module_path` | 模块真实目录，控制台靠它定位 `ctl.sh` |
| `…/modules/badminton_booker/webroot/status.json` | 状态快照，页面直接 fetch 它，不经过 exec |

**改端口或监听地址**：新建 `/data/adb/badminton-booker/config.sh`：

```sh
HOST=0.0.0.0        # 只让本机访问就写 127.0.0.1
PORT=8080
# 想和 Termux 方式共用配置，就指向它的数据目录：
# DATA_DIR=/data/data/com.termux/files/home/badminton-booker/data
```

改完在控制台点「重启」生效。

> 默认数据目录与 Termux 方式是**分开**的。要共用，按上面那行 `DATA_DIR` 指过去即可。

---

## 6. 备份与卸载

```bash
# 备份（需要 root shell，Termux 里 su 后执行）
tar czf /sdcard/booker-$(date +%F).tar.gz -C /data/adb/badminton-booker data
```

- **卸载模块**：KernelSU 管理器里删除即可，会先停掉进程；数据目录**保留**，重装后继续用。
- **手动清理数据**：`rm -rf /data/adb/badminton-booker`（`secret.key` 一并删除，之前保存的会话将无法解密）。

---

## 7. 排查

### 7.1 页面读不到状态

控制台有**两条独立通道**，任意一条通就能显示状态：

| 通道 | 机制 | 依赖 |
| --- | --- | --- |
| 状态快照 | 页面直接 `fetch('status.json')` | 无需 root、无需 exec |
| 命令执行 | `ksu.exec` 调 `ctl.sh` | 需要 WebUI 桥接可用 |

`ctl.sh status` 每次运行都会写一份 `webroot/status.json`，`service.sh` 里还有个每 20 秒的心跳在刷新它。
所以只要模块正常开机，页面**不做任何命令调用**也能显示「运行中 / 已停止、端口、PID」。

**现象 A：横幅写「命令通道不可用」**

先对照官方契约（[module-webui](https://kernelsu.org/zh_CN/guide/module-webui.html)，
实现见 npm 包 `kernelsu` 的 `index.js`）：

```js
// 官方写法：第三参数是「函数名字符串」，不是函数对象
window[callbackName] = (errno, stdout, stderr) => { /* ... */ };
ksu.exec(command, JSON.stringify(options), callbackName);
```

两个参数都必须是**字符串**：`options` 传对象会直接抛
`Error invoking exec: Java exception was raised during method invocation`；
回调传函数对象则原生取不到名字，**永远不会回调**（表现为「超时无响应」）。

`spawn` 同理，四参数：

```js
window[childName] = child;   // child 需带 stdout/stderr 的 on/emit
ksu.spawn(command, JSON.stringify(args), JSON.stringify(options), childName);
// 原生侧：child.stdout.emit("data", chunk) / child.emit("exit", code)
```

模块的 `webroot/index.html` 已按这个契约实现（`ksu-official`），并保留
`ksu-spawn`、`spawn-legacy` 与几种历史写法作为兜底，用 `echo __KSU_OK__` 并发探测后择优。
成功的形态记在 `localStorage`，下次直接用。

点「环境自检」能看到实际结果：

```
exec = ksu-official
ksu 成员: exec,spawn,toast  toast=function  spawn=function
```

- 若全部失败 → 确认模块在 KernelSU 里是「已启用」，且页面是从该模块的网页入口打开的；
  按钮暂时用 Termux 代替：
  ```bash
  su -c 'sh /data/adb/modules/*/ctl.sh start'
  su -c 'sh /data/adb/modules/*/ctl.sh stop'
  ```

> 首屏不等命令通道：状态先用快照渲染，探测在后台进行。

**现象 B：横幅写「读取失败」**

快照没取到，命令也没定位到脚本。按顺序确认：

```bash
su
cat /data/adb/badminton-booker/module_path     # 模块真实目录
ls -l "$(cat /data/adb/badminton-booker/module_path)/ctl.sh"
sh "$(cat /data/adb/badminton-booker/module_path)/ctl.sh" status
cat "$(cat /data/adb/badminton-booker/module_path)/webroot/status.json"
```

- `module_path` 不存在 → `post-fs-data.sh` 与 `service.sh` 都没执行到，检查模块是否启用、是否真的重启过；
- `ctl.sh` 不存在 → 模块包没解压完整，重装一次；
- `status.json` 不存在 → 心跳没跑（看 `service.log`），但此时 `ctl.sh status` 手动执行会立刻生成。

**现象 C：状态一直是「已停止」，但 8080 能打开**

说明进程是 Termux 里手动起的，不是模块起的。模块只认自己的 `booker.pid`。
点「启动」让模块接管，或停掉 Termux 那个实例。

### 7.1b 运行状态怎么显示在模块列表里

KernelSU 的模块列表直接读 `module.prop` 的 `description`，所以 `ctl.sh` 每次刷新状态时
都会把这一行改写成：

```
description=运行中[PID: 18197]
```

停止时改回 `已停止`。只替换这一行，`id` / `name` / `version` 等字段原样保留；
内容没变化就不写文件。模块列表下拉刷新即可看到。

### 7.1c 卡死会自动重启

心跳每 20 秒刷新状态，每 3 次（约 60 秒）做一次健康检查（`ctl.sh watchdog`）：
进程在、但 `/healthz` 连续 3 次不通，就自动结束并重新拉起，日志写进 `service.log`。
手机被系统冻结、或事件循环被卡住时，最多一分钟自动恢复。

另外 `run.py` 加了事件循环探针：卡顿超过 5 秒会把所有线程栈打进 `booker.log`，
下次真出问题可以直接看到卡在哪一行。

### 7.2 页面卡顿

旧版每次刷新都要启动一次 Python 做健康检查，手机上单次 1–4 秒，所以点哪儿都发涩。

现在的分工：

| 动作 | 频率 | 耗时 |
| --- | --- | --- |
| 状态刷新 | 10 秒 | 一次 `fetch`，毫秒级 |
| 健康检查 | 60 秒 / 手动 | 启动 Python 请求 `/healthz`，约 1 秒 |
| 启动 / 停止 | 手动 | 等进程起来并健康检查通过 |

页面不可见时全部暂停。若仍觉得卡，只点「刷新状态」即可。

### 7.3 其他

| 现象 | 处理 |
| --- | --- |
| 运行时显示「缺 Python」 | Termux 里 `pkg install python`，再点「安装依赖」 |
| 点启动后立刻变回停止 | 点「运行日志」看末尾几行；常见是缺依赖或端口被占用 |
| 健康检查非 200 但进程在跑 | 端口被占了：改 `/data/adb/badminton-booker/config.sh` 里的 `PORT` 后重启 |
| 「打开完整界面」没反应 | 系统限制了后台唤起浏览器；页面会显示地址，手动复制打开 |
| 开机后没自动启动 | 看 `/data/adb/badminton-booker/service.log`；确认模块在 KernelSU 里是启用状态 |
| 想同时用 Termux 方式 | 可以，但两者抢同一端口。给一方改 `PORT`，或让 Termux 那边不启动 |
| 卸载后进程还在 | 已修：`uninstall.sh` 会先停心跳再停进程 |

---

## 8. 模块文件说明

```
module.prop          模块信息（名称、版本、描述）
service.sh           开机自启：等 sys.boot_completed 后拉起服务；另起状态心跳
post-fs-data.sh      早期初始化：建数据目录、记录模块真实路径
ctl.sh               控制脚本：start/stop/restart/status/probe/diag/selfcheck/deps/log/where
customize.sh         安装时：设权限、检查 Termux Python
uninstall.sh         卸载时：停心跳与进程（数据保留）
webroot/index.html   KernelSU 网页控制台
webroot/status.json  运行时生成的状态快照（由 ctl.sh status 写入）
app/                 程序本体（与主发布包同一份代码）
```
