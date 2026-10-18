# 07 在 Termux（手机）上运行

两种运行方式之一；另一种是 [08 KernelSU 模块](08-KernelSU%20模块.md)（开机自启 + 网页控制台）。

---

## 1. 运行环境

| 项目 | 要求 |
| --- | --- |
| 设备 | Android 7.0+ |
| Termux | F-Droid 版（Play 版已停更） |
| Python | 3.10+（`pkg install python` 给 3.12） |
| 磁盘 / 内存 | ~100 MB / 256 MB 空闲 |
| 网络 | 能访问 `api.seiue.com`、`passport.seiue.com` |

不需要 root、编译器、Node.js。

---

## 2. 安装

### 2.1 装 Termux 与 Python

从 [F-Droid](https://f-droid.org/packages/com.termux/) 安装 Termux（已装可跳过），然后：

```bash
pkg update -y
pkg install -y python unzip
python -V          # 应显示 3.12.x 或更高
```

### 2.2 把项目放进 Termux

**方式 A：手机浏览器下载（最简单）**

1. 把 `badminton-booker-1.4.0.zip` 传到手机（微信文件传输助手 / 数据线 / 网盘均可）；
2. 在 Termux 里执行一次 `termux-setup-storage`，弹窗点「允许」；
3. 复制并解压：

```bash
cd ~
cp ~/storage/downloads/badminton-booker-1.4.0.zip .
unzip -q badminton-booker-1.4.0.zip
cd badminton-booker
```

> 如果文件在微信的下载目录，路径通常是
> `~/storage/downloads/WeiXin/` 或 `~/storage/shared/tencent/MicroMsg/Download/`，
> 用 `ls ~/storage/downloads` 找一下。

**方式 B：从电脑推（会用 adb 的话最快）**

```bash
# 在电脑上执行
adb push badminton-booker-1.4.0.zip /sdcard/Download/
# 然后在 Termux 里
termux-setup-storage && cd ~ && cp ~/storage/downloads/badminton-booker-1.4.0.zip . && unzip -q badminton-booker-1.4.0.zip
```

**方式 C：直接改代码**（项目已在 git 仓库时）`pkg install git && git clone <仓库地址>`

### 2.3 装依赖

```bash
cd ~/badminton-booker
bash termux-setup.sh
```

脚本会依次：确认 Python 版本 → 升级 pip → 装 `requirements.txt` → **自检能否 import 全部模块并读到时区数据** → 提示要不要装 `termux-api`（用于后台保活）。

国内网络慢的话换镜像：

```bash
PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/ bash termux-setup.sh
```

也可以只装依赖、不做其他事：

```bash
python -m pip install -r requirements.txt
```

**装完后应该看到**（17 个包，全部纯 Python，无编译过程）：

```
starlette  uvicorn  jinja2  markupsafe  python-multipart
httpx  httpcore  h11  anyio  sniffio  idna  certifi
jmespath  itsdangerous  tzdata  click  typing_extensions
```

## 3. 启动

```bash
cd ~/badminton-booker
python run.py
```

启动后会打印：

```
  希悦自动订场  v1.0.0
  ----------------------------------------------
  本机访问    http://127.0.0.1:8080
  局域网访问  http://192.168.1.23:8080
  数据目录    /data/data/com.termux/files/home/badminton-booker/data
  登录口令    未设置（局域网内任何人可访问）
  ----------------------------------------------
  Termux 提示：
    · 先执行 termux-wake-lock，避免息屏后系统杀掉进程
    · 在手机设置里关掉 Termux 的电池优化
    · 停止服务：Ctrl+C（或另开一个会话 pkill -f run.py）
```

常用参数：

```bash
python run.py --port 9000          # 换端口（8080 被占用时也会自动 +1）
python run.py --host 127.0.0.1     # 只允许本机访问，局域网看不到
python run.py --dev                # 改代码自动重载（开发用）
```

等价的原生命令（不想用启动器时）：

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

---

## 4. 访问

| 场景 | 地址 | 说明 |
| --- | --- | --- |
| 手机自己 | `http://127.0.0.1:8080` | 直接在手机浏览器打开 |
| 同一 WiFi 的电脑 | `http://<手机IP>:8080` | 手机 IP 见启动横幅，或「系统设置 → WLAN → 当前网络」 |
| 数据接口 | `http://127.0.0.1:8080/healthz` | 返回 `{"ok":true,"version":"1.4.0"}` 即服务正常 |

在 Termux 里直接唤起浏览器：

```bash
termux-open-url http://127.0.0.1:8080
```

> 想设访问口令：先 `export APP_PASSWORD='你的口令'` 再启动，
> 之后打开任何页面都要先登录（Cookie 有效期 7 天）。

---

## 5. 首次配置（四步）

1. **导入会话**：电脑上在已登录的预约页按 F12 → Network → 找到下单那条 POST →
   右键 *Copy as cURL* → 粘贴到「设置 → ① 一键导入」→ 保存。
2. **同步场地**：「设置 → ②a」点「同步场地」，确认看到 6 块羽毛球场地（8–13 号）。
3. **配令牌续期（强烈建议）**：「设置 → ②b → 会话 Cookie」，
   从 F12 → Application → Cookies → `https://passport.seiue.com` 复制 `Name=Value`，
   点「立即续期并校验」。成功会显示「续期成功，新令牌剩余 86399s（校验 HTTP 200）」。
   > 站点令牌 24 小时过期。不配这一步就得每天重贴一次 cURL。
4. **建任务**：「任务 → 新建」→ 勾选场地（可多选，按优先级依次尝试）、
   填日期与时段（工作日开放 `16:35–18:30`）、人数 2 →
   **先开「演练模式」跑一次**确认请求体无误 → 关掉演练模式，设好开抢时间。

详细的抓包与字段说明见 [04-抓包与配置指南](04-抓包与配置指南.md)。

---

## 6. 后台保活（Termux 特有，重要）

Android 会主动杀后台进程。三个措施缺一不可：

**① 唤醒锁**

```bash
pkg install -y termux-api          # 若 termux-setup.sh 里没装
termux-wake-lock                   # 启动前执行，锁住 CPU 唤醒
```

释放：`termux-wake-unlock`。

**② 关掉电池优化**

系统设置 → 应用 → Termux → 电池 → 选「无限制 / 不受限制」。
（不同厂商路径不同：小米在「省电策略」，华为在「应用启动管理」，OPPO 在「耗电管理」。）

**③ 用 tmux 保住会话**（可选但推荐）

关掉 Termux 界面后进程会挂起，用 tmux 可以保持：

```bash
pkg install -y tmux
tmux new -s booker          # 新建会话
python run.py               # 在里面启动
# 按 Ctrl+B 然后 D 脱离，进程继续跑
tmux attach -t booker       # 下次回到这个会话
```

**④ 开机自启**（可选）

装 [Termux:Boot](https://f-droid.org/packages/com.termux.boot/) 后创建脚本：

```bash
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/booker.sh <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
cd ~/badminton-booker && python run.py >> ~/booker.log 2>&1
EOF
chmod +x ~/.termux/boot/booker.sh
```

---

## 7. 验证清单（复制粘贴逐条跑）

```bash
cd ~/badminton-booker

# 1. Python 版本
python -V

# 2. 依赖能全部 import（不报错即通过）
python -c "import starlette,uvicorn,jinja2,multipart,httpx,jmespath,itsdangerous;print('依赖 OK')"

# 3. 时区数据可用（调度时间依赖它）
python -c "from zoneinfo import ZoneInfo;print(ZoneInfo('Asia/Shanghai'))"

# 4. 没有混进需要编译的旧依赖（应该报 ModuleNotFoundError）
python -c "import fastapi" 2>&1 | tail -1
python -c "import cryptography" 2>&1 | tail -1

# 5. 应用能完成一次冷启动（不监听端口，只跑初始化，2 秒即返回）
python -c "
import asyncio
from app.main import app
from app import store, preset
store.init_db(); preset.ensure_default_templates()
print('初始化 OK，任务数 =', len(store.list_tasks()))
"
```

**功能级验证**（起服务后逐个访问）：

```bash
python run.py &                      # 后台起服务
sleep 5
for p in /healthz / /settings /tasks /runs /templates /tasks/new; do
  printf '%-14s' "$p"
  curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:$p" 2>/dev/null || \
  python -c "import urllib.request,sys;print(urllib.request.urlopen('http://127.0.0.1:8080$p').status)"
done
```

期望：`/healthz` → 200 且返回 JSON；其余页面 → 200（未登录时会 303 跳到 `/login`，也属正常）。

> Android 上一般没有 `curl`，用 `pkg install -y curl` 装，或用上面那条 `python -c` 替代。

**端到端验证**（验证真的能下单，不产生实际订单）：

1. 「任务」新建，勾选「演练模式」，时间设成一分钟后；
2. 观察「运行记录」里出现一条 `dry_run`，`status=200`，且能看到渲染后的请求体；
3. 用它和浏览器 F12 里的真实请求对比 `time_ranges`/`date_ranges`/`capacity` 是否一致；
4. 一致后关掉演练模式。

跑完整测试套件（可选，需要本地假站点，约 30 秒）：

```bash
python tests/smoke_test.py
# 期望最后一行：全部通过：179 项
```

---

## 8. 与在电脑上运行的区别

同一份代码、同一套依赖、同一个启动命令。差别只在「怎么让它一直活着」。

| 项目 | 电脑 / 服务器 | Termux（手机） |
| --- | --- | --- |
| 启动 | `python run.py` | `python run.py` |
| 后台常驻 | systemd（见 [03 文档 4.2](03-依赖与部署.md)） | `termux-wake-lock` + 关闭电池优化（见第 7 节） |
| 开机自启 | `systemctl enable booker` | Termux:Boot（见 7.4） |
| 数据目录 | 项目内 `data/` | `~/badminton-booker/data` |
| 局域网访问 | 直接可用 | 同一 WiFi 下直接可用 |
| 升级 | 覆盖代码 → `systemctl restart booker` | 覆盖代码 → 重跑 `termux-setup.sh` |
| 代码差异 | 无 | 无 |

**数据备份**：`app.db` 与 `secret.key` 必须一起备份，缺了 key 解不开已存的
会话与 Cookie（只能重新粘贴一次）。

```bash
tar czf ~/booker-backup-$(date +%F).tar.gz -C ~/badminton-booker data
```

---

## 9. 排错

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `pip install` 卡在 `Building wheel for pydantic-core` | 用的是旧版 `requirements.txt`（里面还有 fastapi） | 确认 `requirements.txt` 里没有 `fastapi`/`cryptography`，重新解压发布包 |
| `No matching distribution found for starlette` | pip 太旧或镜像没有该版本 | `python -m pip install --upgrade pip`，或换官方源 |
| `externally-managed-environment` | 系统 Python 的 PEP 668 保护 | `python -m pip install --break-system-packages -r requirements.txt` |
| 启动后浏览器打不开 | 服务没起在你以为的端口 | 看启动横幅里的端口；`python run.py --port 9000` 换一个 |
| 局域网其他设备打不开 | 不在同一 WiFi / 路由器开了 AP 隔离 / `--host 127.0.0.1` | 改 `--host 0.0.0.0`；关掉路由器的 AP 隔离 |
| 息屏几分钟后任务没执行 | Android 杀了后台 | 见第 7 节：`termux-wake-lock` + 关电池优化 + tmux |
| 任务时间总是差几小时 | 时区数据缺失 | `python -m pip install -r requirements.txt`（含 `tzdata`），并确认 `APP_TZ=Asia/Shanghai` |
| `Temporary failure in name resolution` | Termux 的 DNS 坏了 | `pkg install -y dnsutils`；或在 `~/.termux/termux.properties` 里配 DNS |
| 请求全部 401 | 令牌过期且没配续期 | 「设置 → ②b」配会话 Cookie，点「立即续期并校验」 |
| 控制台有 `/chalk/classin/personal/account` 404 | 账号未绑定 ClassIn，前端可选分支 | 与令牌无关，忽略 |

**升级到新版本**（保留数据）：

```bash
cd ~
tar czf booker-data-backup.tar.gz -C ~/badminton-booker data
unzip -o badminton-booker-1.4.0.zip     # 覆盖代码，data/ 不在包里，不会被删
cd badminton-booker && bash termux-setup.sh
python run.py
```

---

## 10. 安全提醒

- 程序里存着你的会话 Cookie（等同于登录态），`data/secret.key` 是解密钥匙。
  不要把 `data/` 目录发给别人，也不要把服务暴露到公网。
- 默认监听 `0.0.0.0` 是为了方便局域网访问；如果只在自己手机上用，
  启动时加 `--host 127.0.0.1` 更安全。
- 建议设一个访问口令：`export APP_PASSWORD='xxx'` 后再启动。
- 本项目只操作你自己的账号、只按你配置的场地与时段提交，不做验证码绕过、
  不多账号、不代理池。
