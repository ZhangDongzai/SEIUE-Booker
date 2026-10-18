#!/system/bin/sh
# 安装时执行：设置权限、检查 Termux 的 Python 运行时。

ui_print "- 希悦自动订场 · KernelSU 模块"

chmod 755 "$MODPATH/service.sh" "$MODPATH/post-fs-data.sh" "$MODPATH/ctl.sh" 2>/dev/null
chmod 644 "$MODPATH/module.prop" "$MODPATH/uninstall.sh" 2>/dev/null

mkdir -p /data/adb/badminton-booker/data 2>/dev/null
chmod 700 /data/adb/badminton-booker /data/adb/badminton-booker/data 2>/dev/null

PREFIX=/data/data/com.termux/files/usr
if [ -x "$PREFIX/bin/python" ]; then
    ui_print "- Python 运行时: $("$PREFIX/bin/python" -V 2>&1)"
    if "$PREFIX/bin/python" -c "import starlette, uvicorn, jinja2, httpx" 2>/dev/null; then
        ui_print "- 依赖检查: 已就绪"
    else
        ui_print "- 依赖检查: 缺依赖"
        ui_print "  装完重启后，打开本模块的网页控制台点「安装依赖」即可"
    fi
else
    ui_print "! 未检测到 Termux 的 Python 运行时"
    ui_print "  请先在 Termux 里执行: pkg install python"
    ui_print "  然后再回到网页控制台点「安装依赖」"
fi

ui_print "- 安装到: $MODPATH"
ui_print "- 程序目录: $MODPATH/app"
ui_print "- 数据目录: /data/adb/badminton-booker/data"
ui_print "- 网页控制台会读取 /data/adb/badminton-booker/module_path 定位本模块"
ui_print "- 重启后自动启动；也可在网页控制台手动启动/停止"
