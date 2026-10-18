#!/system/bin/sh
# 开机自启：等系统完全启动后再拉起服务，并维持一份状态快照供网页控制台读取。
#
# Android 16 / HyperOS 上 sys.boot_completed 可能明显晚于模块脚本，
# 所以这里先等属性，再额外等一会儿让网络与存储就绪。

MODDIR=${0%/*}
[ "$MODDIR" = "$0" ] && MODDIR="."      # $0 里没有斜杠，说明是相对调用
case "$MODDIR" in
    /*) ;;
    *) MODDIR=$(cd "$MODDIR" 2>/dev/null && pwd) ;;
esac
[ -z "$MODDIR" ] && MODDIR="."

RUN_DIR=/data/adb/badminton-booker
mkdir -p "$RUN_DIR" 2>/dev/null

# 记下真实路径：模块目录名不一定等于 module.prop 里的 id，
# 网页控制台靠这个文件定位 ctl.sh，不靠猜。
echo "$MODDIR" > "$RUN_DIR/module_path" 2>/dev/null
chmod 644 "$RUN_DIR/module_path" 2>/dev/null

# 日志长期累积会占空间，启动时截断一次
if [ -f "$RUN_DIR/service.log" ] && [ "$(wc -c < "$RUN_DIR/service.log" 2>/dev/null || echo 0)" -gt 262144 ]; then
    tail -n 400 "$RUN_DIR/service.log" > "$RUN_DIR/service.log.tmp" 2>/dev/null && mv "$RUN_DIR/service.log.tmp" "$RUN_DIR/service.log"
fi

echo "===== $(date '+%Y-%m-%d %H:%M:%S') service.sh 启动, MODDIR=$MODDIR =====" >> "$RUN_DIR/service.log"

# 先写一次快照，保证页面开机后立刻有状态可显示
sh "$MODDIR/ctl.sh" status >/dev/null 2>&1

# 心跳：每 20 秒刷新一次 status.json。页面直接 fetch 这个文件，
# 不经过 ksu.exec，因此命令通道不兼容时也能看到正确状态。
(
    i=0
    while :; do
        sh "$MODDIR/ctl.sh" status >/dev/null 2>&1
        i=$((i + 1))
        # 每 3 个心跳（约 60 秒）做一次健康检查；卡死就自动重启
        if [ $((i % 3)) -eq 0 ]; then
            sh "$MODDIR/ctl.sh" watchdog
        fi
        sleep 20
    done
) < /dev/null >> "$RUN_DIR/service.log" 2>&1 &
echo $! > "$RUN_DIR/heartbeat.pid"

# 最多等 5 分钟，避免开机异常时永久挂起
i=0
while [ "$(getprop sys.boot_completed)" != "1" ] && [ $i -lt 150 ]; do
    sleep 2
    i=$((i + 1))
done

# 留出时间让网络可用（校园网 / Wi-Fi 握手）
sleep 15

# 已经在跑（例如用户手动起的）就不要重复启动
if sh "$MODDIR/ctl.sh" status 2>/dev/null | grep -q "^STATE=running"; then
    echo "已在运行，跳过" >> "$RUN_DIR/service.log"
    exit 0
fi

sh "$MODDIR/ctl.sh" start >> "$RUN_DIR/service.log" 2>&1
sh "$MODDIR/ctl.sh" status >/dev/null 2>&1
echo "已完成，结果见 ctl.sh 输出" >> "$RUN_DIR/service.log"
