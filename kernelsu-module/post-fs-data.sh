#!/system/bin/sh
# 早期初始化：建目录，并把模块的真实路径记录下来。
#
# 为什么记路径：KernelSU 装完之后的模块目录名不一定等于 module.prop 里的 id
# （取决于安装方式与版本）。网页控制台读这个文件就能找到 ctl.sh，不必猜目录名。

MODDIR=${0%/*}
[ "$MODDIR" = "$0" ] && MODDIR="."      # $0 里没有斜杠，说明是相对调用
case "$MODDIR" in
    /*) ;;
    *) MODDIR=$(cd "$MODDIR" 2>/dev/null && pwd) ;;
esac
[ -z "$MODDIR" ] && MODDIR="."

RUN_DIR=/data/adb/badminton-booker
mkdir -p "$RUN_DIR/data" 2>/dev/null
chmod 700 "$RUN_DIR" 2>/dev/null
chmod 700 "$RUN_DIR/data" 2>/dev/null

# 供网页控制台定位模块（内容是一行绝对路径）
echo "$MODDIR" > "$RUN_DIR/module_path" 2>/dev/null
chmod 644 "$RUN_DIR/module_path" 2>/dev/null

echo "===== $(date '+%Y-%m-%d %H:%M:%S') post-fs-data: MODDIR=$MODDIR =====" >> "$RUN_DIR/service.log"
