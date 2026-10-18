#!/usr/bin/env bash
# Termux（以及普通 Linux / macOS）一键安装脚本。
#
# 用法：
#   bash termux-setup.sh
#   PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/ bash termux-setup.sh   # 国内加速
#
# 做的事只有三件：确认 Python ≥3.10、装纯 Python 依赖、自检能否 import。
# 全程不需要编译器、不需要 root。
set -eu

cd "$(dirname "$0")"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*" >&2; exit 1; }

is_termux() { [ -d /data/data/com.termux/files/usr ]; }

# ---------------------------------------------------------------- 1. Python
say "检查 Python"
if ! command -v python >/dev/null 2>&1; then
    if is_termux || command -v pkg >/dev/null 2>&1; then
        warn "未找到 python，正在用 pkg 安装"
        pkg update -y
        pkg install -y python
    else
        die "未找到 python，请先安装 Python 3.10+"
    fi
fi

python - <<'PY' || die "Python 版本不满足要求（需要 3.10+）"
import sys
if sys.version_info < (3, 10):
    raise SystemExit(f"当前 {sys.version.split()[0]}，需要 3.10+")
print("   Python " + sys.version.split()[0])
PY

# ---------------------------------------------------------------- 2. pip
say "准备 pip"
python -m pip install --quiet --upgrade pip 2>/dev/null || warn "pip 升级失败，继续用现有版本"

PIP_ARGS="--no-cache-dir"
if [ -n "${PIP_INDEX:-}" ]; then
    PIP_ARGS="$PIP_ARGS -i $PIP_INDEX"
    echo "   使用镜像：$PIP_INDEX"
fi
# 部分发行版 PEP 668 会拒绝往系统环境装包
if python -m pip install --help 2>/dev/null | grep -q -- "--break-system-packages"; then
    PIP_ARGS="$PIP_ARGS --break-system-packages"
fi

# ---------------------------------------------------------------- 3. 依赖
say "安装依赖（全部纯 Python，无需编译）"
# shellcheck disable=SC2086
python -m pip install $PIP_ARGS -r requirements.txt

# ---------------------------------------------------------------- 4. 自检
say "自检"
python - <<'PY' || die "依赖不完整，请把上面的报错发出来"
import importlib
need = ["starlette", "uvicorn", "jinja2", "multipart", "httpx",
        "jmespath", "itsdangerous", "tzdata", "zoneinfo"]
missing = []
for name in need:
    try:
        importlib.import_module(name)
    except Exception as exc:            # noqa: BLE001
        missing.append(f"{name}({exc.__class__.__name__})")
if missing:
    print("   缺少：" + ", ".join(missing))
    raise SystemExit(1)

from zoneinfo import ZoneInfo
ZoneInfo("Asia/Shanghai")               # 时区数据必须可用，否则调度时间会算错
print("   依赖齐全，时区数据可用")
PY

# ---------------------------------------------------------------- 5. Termux 保活
if is_termux; then
    say "Termux 后台保活"
    if ! command -v termux-wake-lock >/dev/null 2>&1; then
        warn "未安装 termux-api：息屏后系统可能会杀掉程序"
        if [ -t 0 ]; then
            printf '   现在安装？(y/N) '
            read -r ans || ans=""
            if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
                pkg install -y termux-api || warn "安装失败，可稍后手动 pkg install termux-api"
            fi
        fi
    fi
    if command -v termux-wake-lock >/dev/null 2>&1; then
        termux-wake-lock && echo "   已获取唤醒锁（可手动 termux-wake-unlock 释放）"
    fi
fi

# ---------------------------------------------------------------- 完成
say "安装完成"
cat <<'EOF'
   启动服务：   python run.py
   浏览器打开： http://127.0.0.1:8080
   换端口：     python run.py --port 9000
   局域网访问： 启动后按屏幕提示里的「局域网访问」地址
   详细说明：   docs/07-Termux 部署.md
EOF
