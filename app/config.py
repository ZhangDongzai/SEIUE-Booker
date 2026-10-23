"""集中配置：所有可变项都从环境变量读取，代码里不出现硬编码路径。"""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

APP_NAME = "希悦自动订场"
APP_VERSION = "1.0.1"

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent

DATA_DIR = Path(os.getenv("DATA_DIR") or (PROJECT_DIR / "data")).resolve()
DB_PATH = str(DATA_DIR / "app.db")
KEY_PATH = str(DATA_DIR / "secret.key")

TZ_NAME = os.getenv("APP_TZ", "Asia/Shanghai")
TZ = ZoneInfo(TZ_NAME)

APP_PASSWORD = (os.getenv("APP_PASSWORD") or "").strip()
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "10"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# 监听地址。默认 0.0.0.0：手机浏览器用 127.0.0.1 访问，同一局域网的其他设备用手机 IP 访问。
# 只想本机可见时设 APP_HOST=127.0.0.1。
APP_HOST = (os.getenv("APP_HOST") or "0.0.0.0").strip()
APP_PORT = int(os.getenv("APP_PORT", "8080"))

# 默认直连，不读取系统 HTTP(S)_PROXY（系统里常被注入，会导致请求串错）。
# 确需代理时显式设置 APP_PROXY=http://host:port
PROXY = (os.getenv("APP_PROXY") or "").strip()

# 校园站点（前端壳，用于时钟校准）
SITE = os.getenv("SITE_BASE", "https://chalk-c3.seiue.com").rstrip("/")
# 业务接口域名（真实数据面，鉴权走 Authorization: Bearer）
API = os.getenv("API_BASE", "https://api.seiue.com").rstrip("/")

# 场地清单：可列出本校全部「可预约场地」，含 max_order_days / need_approval 等元信息
VENUES_URL = API + "/scms/venue/order-venues"
# 会话自检：带上有效 Bearer 返回 200，无/失效返回 401
DEFAULT_CHECK_URL = os.getenv("CHECK_URL") or (API + "/scms/venue/order-venues")

# 单轮执行的硬性时限（秒），避免异常情况下无限重试
MAX_RUN_SECONDS = 120

# 站点预约窗口：最多提前多少天（场地清单里的 max_order_days 实测为 7）。
# 目标日期在窗口内 → 立即执行；不在 → 等到窗口开启那天再执行。
MAX_ORDER_DAYS = int(os.getenv("MAX_ORDER_DAYS", "7"))
# 窗口内执行未成功时的重试间隔（秒）
RETRY_SECONDS = int(os.getenv("RETRY_SECONDS", "600"))

# ---------------- 令牌自动续期（refresh_token 模式） ----------------
# 实测：POST 到该地址，必须 application/x-www-form-urlencoded（JSON 会 400）
OAUTH_TOKEN_URL = (os.getenv("OAUTH_TOKEN_URL") or (API + "/chalk/oauth/tokens")).strip()
# 前端明文里的公共 client（web/app 各自的 id+secret，可在环境变量里覆盖）
OAUTH_CLIENT_ID = (os.getenv("OAUTH_CLIENT_ID") or "bEGAfhMhb2OZ2yARn8TBxg").strip()
OAUTH_CLIENT_SECRET = (os.getenv("OAUTH_CLIENT_SECRET") or "8TCt6gW9gwvFnc").strip()
# 剩余有效期低于该秒数才换（前端自己用的也是 600 秒）
TOKEN_REFRESH_SKEW = int(os.getenv("TOKEN_REFRESH_SKEW", "600"))
# 后台巡检间隔：多久检查一次令牌是否该续期
TOKEN_CHECK_SECONDS = int(os.getenv("TOKEN_CHECK_SECONDS", "300"))

# ---------------- 免密换令牌（passport 会话 cookie） ----------------
# 前端源码实测：Web 端不保存 refresh_token，令牌将过期时走的是
#   POST {PASSPORT}/authorize   form: response_type=token&client_id=<web client>
#   withCredentials: true（依赖 passport 域的 HttpOnly 会话 cookie）
# 无 cookie 时返回 401 {"name":"Unauthorized","message":"Login required"}。
# 所以存下那个 cookie，就能在令牌快过期时免密码换新的。
PASSPORT_AUTHORIZE_URL = (os.getenv("PASSPORT_AUTHORIZE_URL")
                          or "https://passport.seiue.com/authorize").strip()
PASSPORT_WEB_CLIENT_ID = (os.getenv("PASSPORT_WEB_CLIENT_ID")
                          or "GpxvnjhVKt56qTmnPWH1sA").strip()

# 身份类请求头以「设置」里导入的会话为唯一来源，模板里的同名字头不覆盖
SESSION_AUTHORITATIVE = ("authorization", "cookie")


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
