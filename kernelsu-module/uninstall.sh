#!/system/bin/sh
# 卸载模块时停止进程与心跳；数据目录 /data/adb/badminton-booker 保留，重装后可直接续用。

RUN_DIR=/data/adb/badminton-booker

# 先停心跳，否则它每 20 秒还会跑一次 status
if [ -f "$RUN_DIR/heartbeat.pid" ]; then
    kill "$(cat "$RUN_DIR/heartbeat.pid")" 2>/dev/null
    rm -f "$RUN_DIR/heartbeat.pid"
fi

if [ -f "$RUN_DIR/booker.pid" ]; then
    pid=$(cat "$RUN_DIR/booker.pid" 2>/dev/null)
    if [ -n "$pid" ]; then
        kill "$pid" 2>/dev/null
        sleep 1
        kill -9 "$pid" 2>/dev/null
    fi
    rm -f "$RUN_DIR/booker.pid"
fi
