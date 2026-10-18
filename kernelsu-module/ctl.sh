#!/system/bin/sh
# 希悦自动订场 · KernelSU 模块控制脚本
#
#   sh ctl.sh start | stop | restart | status | probe | watchdog | diag | selfcheck | deps | install-deps | log [行数] | where
#
# 约定：
#   · status 必须极快（只读 pid 文件 + 校验 /proc），供界面高频刷新，不做任何网络/子进程重活
#   · probe 才做 HTTP 健康检查（要启动一次 Python，约 1 秒），由界面按需触发
#   · 任何分支都要有输出，避免界面拿不到 STATE 而显示「未知」

MODDIR=${0%/*}
[ "$MODDIR" = "$0" ] && MODDIR="."      # $0 里没有斜杠，说明是相对调用
case "$MODDIR" in
    /*) ;;
    *) MODDIR=$(cd "$MODDIR" 2>/dev/null && pwd) ;;
esac
[ -z "$MODDIR" ] && MODDIR="."

# ---------------- 默认配置（可在 /data/adb/badminton-booker/config.sh 覆盖） ----------------
PREFIX=${PREFIX:-/data/data/com.termux/files/usr}
TERMUX_HOME=${TERMUX_HOME:-/data/data/com.termux/files/home}
APP_DIR=${APP_DIR:-$MODDIR/app}
DATA_DIR=${DATA_DIR:-/data/adb/badminton-booker/data}
RUN_DIR=${RUN_DIR:-/data/adb/badminton-booker}
HOST=${HOST:-0.0.0.0}
PORT=${PORT:-8080}

[ -f "$RUN_DIR/config.sh" ] && . "$RUN_DIR/config.sh"

PIDFILE="$RUN_DIR/booker.pid"
LOGFILE="$RUN_DIR/booker.log"
PY="$PREFIX/bin/python"

mkdir -p "$RUN_DIR" "$DATA_DIR" 2>/dev/null

prepare_env() {
    export HOME="$TERMUX_HOME"
    export PREFIX
    export LD_LIBRARY_PATH="$PREFIX/lib"
    export PATH="$PREFIX/bin:/system/bin:/system/xbin"
    export TMPDIR="$PREFIX/tmp"
    export PYTHONUNBUFFERED=1
    export APP_TZ=Asia/Shanghai
    export DATA_DIR
}

# 进程存在 + 确实是本程序（防止 pid 被系统复用造成误判）
running_pid() {
    [ -f "$PIDFILE" ] || return 1
    p=$(cat "$PIDFILE" 2>/dev/null)
    [ -n "$p" ] || return 1
    kill -0 "$p" 2>/dev/null || return 1
    cmd=$(tr -d '\000' < "/proc/$p/cmdline" 2>/dev/null)
    case "$cmd" in
        *run.py*) echo "$p"; return 0 ;;
    esac
    return 1
}

# 有 timeout 就用（Android 9+ 的 toybox 自带），避免任何一步把整个调用挂死
tmo() {
    secs="$1"; shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "$secs" "$@"
    else
        "$@"
    fi
}

http_code() {
    prepare_env
    [ -x "$PY" ] || { echo ""; return; }
    tmo 6 "$PY" - "$PORT" <<'PYEOF' 2>/dev/null
import sys, urllib.request
port = sys.argv[1]
try:
    with urllib.request.urlopen("http://127.0.0.1:%s/healthz" % port, timeout=2) as r:
        print(r.status)
except Exception:
    print("")
PYEOF
}

# ---------------------------------------------------------------- 状态快照
# 网页控制台优先直接 fetch 这份文件（不经过 exec），所以即使命令通道不兼容，
# 页面照样能显示正确的运行状态。内容与 status 的 KEY=VALUE 一一对应。
# KernelSU 的模块列表直接读 module.prop 的 description，
# 所以把运行状态写进去，模块页面就能看到「运行中[PID: 18197]」。
# 只替换 description 这一行，其余字段原样保留；内容没变就不写，避免无谓 IO。
update_desc() {
    prop="$MODDIR/module.prop"
    [ -f "$prop" ] || return 0
    [ -w "$prop" ] || return 0
    want="$1"
    have=$(grep -m1 '^description=' "$prop" 2>/dev/null | sed 's/^description=//')
    [ "$have" = "$want" ] && return 0

    tmp="$prop.tmp.$$"
    grep -v '^description=' "$prop" > "$tmp" 2>/dev/null || { rm -f "$tmp"; return 0; }
    printf 'description=%s\n' "$want" >> "$tmp"
    mv "$tmp" "$prop" 2>/dev/null || { rm -f "$tmp"; return 0; }
    chmod 644 "$prop" 2>/dev/null
    return 0
}

write_snapshot() {
    if [ "$1" = "running" ]; then
        update_desc "运行中[PID: $2]"
    else
        update_desc "已停止"
    fi

    wdir="$MODDIR/webroot"
    [ -d "$wdir" ] || return 0
    [ -w "$wdir" ] || return 0
    tmp="$wdir/.status.tmp"
    printf '{"STATE":"%s","PID":"%s","PORT":"%s","HOST":"%s","PY_READY":"%s","APP_READY":"%s","DATA":"%s","AT":"%s"}\n' \
        "$1" "$2" "$PORT" "$HOST" "$3" "$4" "$DATA_DIR" "$(date '+%Y-%m-%d %H:%M:%S')" \
        > "$tmp" 2>/dev/null || return 0
    mv "$tmp" "$wdir/status.json" 2>/dev/null
    chmod 644 "$wdir/status.json" 2>/dev/null
    return 0
}

# ---------------------------------------------------------------- 快路径
do_status() {
    pid=$(running_pid) || pid=""
    if [ -n "$pid" ]; then
        state="running"
    else
        state="stopped"
    fi
    if [ -x "$PY" ]; then py_ready="yes"; else py_ready="no"; fi
    if [ -f "$APP_DIR/run.py" ]; then app_ready="yes"; else app_ready="no"; fi

    # 先落快照再输出：页面优先读快照，这条路径没有子进程、毫秒级
    write_snapshot "$state" "$pid" "$py_ready" "$app_ready"

    echo "STATE=$state"
    echo "PID=$pid"
    echo "PORT=$PORT"
    echo "HOST=$HOST"
    echo "APP=$APP_DIR"
    echo "DATA=$DATA_DIR"
    echo "LOG=$LOGFILE"
    echo "PY_READY=$py_ready"
    echo "APP_READY=$app_ready"
    echo "AT=$(date '+%Y-%m-%d %H:%M:%S')"
}

do_probe() {
    echo "HTTP=$(http_code)"
}

# 看门狗：进程在、但健康检查连续失败，就重启它。
# 手机上的「进程被冻结 / 卡死」往往表现为端口还在、请求永远不回，
# 这时只有重启能恢复，而心跳每 20 秒就会调一次这里。
do_watchdog() {
    pid=$(running_pid) || { rm -f "$RUN_DIR/health_fail"; echo "STATE=stopped"; return 0; }
    # 没有 Python 就测不了健康状态，重启也没意义
    [ -x "$PY" ] || { echo "STATE=running SKIP=no-python"; return 0; }
    code=$(http_code)
    if [ "$code" = "200" ]; then
        rm -f "$RUN_DIR/health_fail"
        echo "STATE=running HTTP=200"
        return 0
    fi

    n=$(cat "$RUN_DIR/health_fail" 2>/dev/null || echo 0)
    case "$n" in ''|*[!0-9]*) n=0 ;; esac
    n=$((n + 1))
    echo "$n" > "$RUN_DIR/health_fail"
    echo "STATE=running HTTP=${code:-none} FAILS=$n"

    if [ "$n" -ge 3 ]; then
        echo "健康检查连续 $n 次失败，重启进程"
        rm -f "$RUN_DIR/health_fail"
        do_stop >/dev/null 2>&1
        do_start
    fi
}

# 纯 shell、不启动任何子进程，用于「环境自检」——保证不会被 Python 拖住
do_selfcheck() {
    echo "SELF=$MODDIR/ctl.sh"
    echo "MODDIR=$MODDIR"
    echo "APP=$APP_DIR"
    echo "DATA=$DATA_DIR"
    if [ -x "$PY" ]; then echo "PY_READY=yes"; else echo "PY_READY=no"; fi
    if [ -f "$APP_DIR/run.py" ]; then echo "APP_READY=yes"; else echo "APP_READY=no"; fi
    echo "ID=$(id 2>/dev/null)"
    echo "--- module_path ---"
    cat "$RUN_DIR/module_path" 2>/dev/null
    echo "--- /data/adb/modules ---"
    ls -1 /data/adb/modules 2>/dev/null | head -30
    echo "--- ctl.sh 候选 ---"
    ls -1 /data/adb/modules/*/ctl.sh 2>/dev/null
    echo "--- 本目录 ---"
    ls -1 "$MODDIR" 2>/dev/null | head -20
    echo "--- webroot ---"
    ls -l "$MODDIR/webroot" 2>/dev/null | head -10
}

do_where() {
    echo "SELF=$MODDIR/ctl.sh"
    echo "MODDIR=$MODDIR"
    echo "APP=$APP_DIR"
}

# ---------------------------------------------------------------- 诊断
do_diag() {
    prepare_env
    echo "SELF=$MODDIR/ctl.sh"
    echo "MODDIR=$MODDIR"
    echo "APP=$APP_DIR"
    if [ -f "$APP_DIR/run.py" ]; then echo "APP_READY=yes"; else echo "APP_READY=no"; fi
    echo "DATA=$DATA_DIR"
    echo "PY=$PY"
    if [ -x "$PY" ]; then
        echo "PY_READY=yes"
        pyver=$(tmo 8 "$PY" -c 'import sys;print(sys.version.split()[0])' 2>/dev/null)
        echo "PY_VERSION=$pyver"
    else
        echo "PY_READY=no"
        echo "PY_VERSION="
    fi
    if [ -x "$PY" ] && tmo 25 "$PY" -c "import starlette, uvicorn, jinja2, multipart, httpx, jmespath, itsdangerous, tzdata" 2>/dev/null; then
        echo "DEPS=ok"
    else
        echo "DEPS=missing"
    fi
    pid=$(running_pid) || pid=""
    if [ -n "$pid" ]; then echo "STATE=running"; else echo "STATE=stopped"; fi
    echo "PID=$pid"
    echo "HTTP=$(http_code)"
    echo "--- /data/adb/modules ---"
    ls -1 /data/adb/modules 2>/dev/null
    echo "--- 最近日志 ---"
    tail -n 8 "$LOGFILE" 2>/dev/null
}

# ---------------------------------------------------------------- 启停
do_start() {
    prepare_env
    pid=$(running_pid) && { echo "already running, pid=$pid"; return 0; }
    [ -x "$PY" ] || { echo "缺少 Python：请先在 Termux 里执行 pkg install python"; return 1; }
    [ -f "$APP_DIR/run.py" ] || { echo "程序目录不完整：$APP_DIR 下找不到 run.py"; return 1; }

    cd "$APP_DIR" || return 1
    echo "===== $(date '+%Y-%m-%d %H:%M:%S') start =====" >> "$LOGFILE"
    nohup "$PY" run.py --host "$HOST" --port "$PORT" --strict-port >> "$LOGFILE" 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    rm -f "$RUN_DIR/health_fail"
    # 降低被系统回收的概率（值越小越不容易被杀）
    echo -1000 > "/proc/$!/oom_score_adj" 2>/dev/null

    i=0
    while [ $i -lt 15 ]; do
        sleep 1
        if ! running_pid >/dev/null; then
            echo "启动失败，最近日志："
            tail -n 15 "$LOGFILE" 2>/dev/null
            rm -f "$PIDFILE"
            return 1
        fi
        code=$(http_code)
        [ "$code" = "200" ] && { echo "started, pid=$(cat $PIDFILE), http=$code"; return 0; }
        i=$((i + 1))
    done
    echo "进程已起但健康检查未通过（http=$code），请看日志"
    return 1
}

do_stop() {
    pid=$(running_pid) || { echo "not running"; rm -f "$PIDFILE"; return 0; }
    kill "$pid" 2>/dev/null
    i=0
    while kill -0 "$pid" 2>/dev/null && [ $i -lt 10 ]; do
        sleep 1
        i=$((i + 1))
    done
    kill -9 "$pid" 2>/dev/null
    rm -f "$PIDFILE"
    echo "stopped"
}

do_deps() {
    prepare_env
    if [ -x "$PY" ]; then echo "PYTHON=ok"; else echo "PYTHON=missing"; return 1; fi
    if [ -f "$APP_DIR/run.py" ]; then echo "APP=ok"; else echo "APP=missing"; return 1; fi
    if "$PY" -c "import starlette, uvicorn, jinja2, multipart, httpx, jmespath, itsdangerous, tzdata" 2>/dev/null; then
        echo "DEPS=ok"
    else
        echo "DEPS=missing"
        return 1
    fi
    echo "OK=1"
}

do_install_deps() {
    prepare_env
    [ -x "$PY" ] || { echo "缺少 Python：请先在 Termux 里执行 pkg install python"; return 1; }
    [ -f "$APP_DIR/requirements.txt" ] || { echo "找不到 requirements.txt"; return 1; }
    cd "$APP_DIR" || return 1
    "$PY" -m pip install --no-cache-dir -r requirements.txt >> "$LOGFILE" 2>&1
    rc=$?
    [ $rc -eq 0 ] && echo "依赖安装完成" || { echo "依赖安装失败，请看日志"; tail -n 15 "$LOGFILE"; }
    return $rc
}

case "$1" in
    start)        do_start ;;
    stop)         do_stop ;;
    restart)      do_stop; do_start ;;
    status)       do_status ;;
    probe)        do_probe ;;
    watchdog)     do_watchdog ;;
    diag)         do_diag ;;
    selfcheck)    do_selfcheck ;;
    where)        do_where ;;
    deps)         do_deps ;;
    install-deps) do_install_deps ;;
    log)          tail -n "${2:-40}" "$LOGFILE" 2>/dev/null || echo "(暂无日志)" ;;
    *)
        echo "用法: sh ctl.sh {start|stop|restart|status|probe|watchdog|diag|selfcheck|deps|install-deps|log [行数]|where}"
        exit 2
        ;;
esac
