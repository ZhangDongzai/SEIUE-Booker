#!/usr/bin/env python
"""本地启动器：Termux / 裸机 / 任何装了 Python 的地方都能直接跑，无需容器或外部服务。

    python run.py                 # 默认 0.0.0.0:8080
    python run.py --port 9000
    python run.py --host 127.0.0.1 # 只允许本机访问
    python run.py --dev            # 改代码自动重载（开发用）
    python run.py --strict-port    # 端口被占用就报错（服务化部署用）

等价于直接执行 `uvicorn app.main:app --host 0.0.0.0 --port 8080`，
只是多了端口占用检测、局域网地址提示和 Termux 专属提醒。
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TERMUX_PREFIX = "/data/data/com.termux"


def is_termux() -> bool:
    return os.environ.get("PREFIX", "").startswith(TERMUX_PREFIX) or (
        Path(TERMUX_PREFIX).exists() and sys.platform.startswith("linux")
    )


def _is_private(ip: str) -> bool:
    """是否为常见的局域网地址（10/8、172.16/12、192.168/16）。"""
    try:
        a, b = (int(x) for x in ip.split(".")[:2])
    except (ValueError, IndexError):
        return False
    return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)


def _reachable(ip: str, target: tuple[str, int] = ("223.5.5.5", 443),
               timeout: float = 0.4) -> bool:
    """从指定网卡地址能否连到公网（用来排除 VMware / Hyper-V 这类虚拟网卡）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.bind((ip, 0))
        sock.connect(target)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def lan_ip() -> str:
    """取本机在局域网里的地址。

    代理软件（Clash/V2Ray）会装一张 198.18.0.0/15 的虚拟网卡并抢走默认路由，
    虚拟机软件也会挂 192.168.x.1 的 host-only 网卡。直接读「默认出口 IP」
    可能拿到这些地址，用户照着访问必然失败。所以这里收集候选后依次筛：
    默认出口是私有网段 → 直接用；否则挑一个真能连外网的网卡地址。
    """
    candidates: list[str] = []

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 80))
        candidates.append(sock.getsockname()[0])
    except OSError:
        pass
    finally:
        sock.close()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = str(info[4][0])
            if ip not in candidates:
                candidates.append(ip)
    except OSError:
        pass

    # 默认出口本身就是局域网地址（家用/办公网络最常见），不用再挑
    if candidates and _is_private(candidates[0]):
        return candidates[0]

    privates = [ip for ip in candidates if _is_private(ip)]
    for ip in privates[:4]:
        if _reachable(ip):
            return ip
    if privates:
        return privates[0]
    for ip in candidates:
        if not ip.startswith(("127.", "169.254.", "0.")):
            return ip
    return ""


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def pick_port(host: str, want: int, tries: int = 20) -> int:
    for offset in range(tries):
        candidate = want + offset
        if port_free(host, candidate):
            if offset:
                print(f"  端口 {want} 已被占用，改用 {candidate}", flush=True)
            return candidate
    raise SystemExit(f"端口 {want}–{want + tries - 1} 都被占用，请用 --port 指定其他端口")


def banner(port: int, data_dir: Path) -> None:
    from app import config

    host_ip = lan_ip()
    lines = [
        "",
        "  " + config.APP_NAME + "  v" + config.APP_VERSION,
        "  " + "-" * 46,
        f"  本机访问    http://127.0.0.1:{port}",
    ]
    if host_ip:
        lines.append(f"  局域网访问  http://{host_ip}:{port}")
    lines += [
        f"  数据目录    {data_dir}",
        f"  登录口令    {'已设置' if config.APP_PASSWORD else '未设置（局域网内任何人可访问）'}",
        "  " + "-" * 46,
    ]
    if is_termux():
        lines += [
            "  Termux 提示：",
            "    · 先执行 termux-wake-lock，避免息屏后系统杀掉进程",
            "    · 在手机设置里关掉 Termux 的电池优化",
            "    · 停止服务：Ctrl+C（或另开一个会话 pkill -f run.py）",
        ]
    else:
        lines.append("  停止服务：Ctrl+C")
    lines += ["", "  浏览器打开上面的地址即可开始配置。", ""]
    print("\n".join(lines), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="希悦自动订场 · 本地启动器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default=None, help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=None, help="监听端口，默认 8080")
    parser.add_argument("--dev", action="store_true", help="开发模式：改代码自动重载")
    parser.add_argument("--strict-port", action="store_true",
                        help="端口被占用时直接报错退出，而不是自动换一个（服务化部署建议加）")
    parser.add_argument("--log-level", default=None, help="uvicorn 日志级别，默认 info")
    args = parser.parse_args()

    from app import config

    host = args.host or config.APP_HOST
    want = args.port or config.APP_PORT
    if args.strict_port:
        if not port_free(host, want):
            raise SystemExit(f"端口 {want} 已被占用（--strict-port 模式不自动换端口）")
        port = want
    else:
        port = pick_port(host, want)
    # 让 config 里的值等于实际监听值，否则启动日志会显示默认 8080，看着像没生效
    config.APP_HOST, config.APP_PORT = host, port
    banner(port, config.DATA_DIR)

    try:
        import uvicorn
    except ImportError:
        raise SystemExit(
            "缺少 uvicorn。先安装依赖：\n"
            "    python -m pip install -r requirements.txt"
        )

    from app.main import app

    if args.dev:
        uvicorn.run("app.main:app", host=host, port=port, reload=True,
                    log_level=args.log_level or "info")
    else:
        uvicorn.run(
            app, host=host, port=port, log_level=args.log_level or "info",
            access_log=False,
            # 手机上连接容易半死不活：短 keep-alive + 有限并发，避免请求堆在队列里
            timeout_keep_alive=5,
            limit_concurrency=64,
            backlog=64,
        )


if __name__ == "__main__":
    main()
