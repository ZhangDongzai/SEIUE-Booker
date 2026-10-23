#!/usr/bin/env python3
"""把项目打包成可刷入的 KernelSU 模块（zip）。

    python build-module.py
    python build-module.py --out /tmp            # 输出到指定目录
    python build-module.py --version v1.0.2      # 临时覆盖版本号（只影响包名，不改文件）
    python build-module.py --verify-only         # 只校验现有包，不重新打包

产出
----
    dist/badminton-booker-ksu-<version>.zip

包内结构（KernelSU 要求 module.prop 与 webroot 在 zip 根目录）：
    module.prop  service.sh  post-fs-data.sh  customize.sh  uninstall.sh
    ctl.sh  webroot/index.html
    app/            ← 程序本体：app 包 + run.py + requirements.txt

为什么内嵌一份 app/
-------------------
模块装到 /data/adb/modules/<id>/ 后要能独立运行，不能依赖 Termux 里的项目目录。
所以每次打包都把当前代码复制进去，保证模块里的程序和你本地这份一致。

平台注意
--------
脚本只用标准库，Windows / Linux / macOS 都能跑；
所有 .sh / .prop / .html 会强制转成 LF——否则 Android 上 `sh` 会报 `\r: not found`。
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULE_DIR = ROOT / "kernelsu-module"
APP_DIR = ROOT / "app"

# 必须放在 zip 根目录的模块文件
MODULE_FILES = [
    "module.prop",
    "service.sh",
    "post-fs-data.sh",
    "customize.sh",
    "uninstall.sh",
    "ctl.sh",
    "webroot/index.html",
]

# 需要转成 LF 的文本类型
LF_SUFFIXES = {".sh", ".prop", ".html"}

# 程序本体里要带上的文件类型（页面、样式、脚本、代码）
APP_SUFFIXES = {".py", ".html", ".css", ".js"}
APP_EXTRA = ["run.py", "requirements.txt"]


# --------------------------------------------------------------------- 工具

def read_prop(path: Path) -> dict[str, str]:
    """解析 module.prop（key=value，一行一个）。"""
    data: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip()
    return data


def copy_as_lf(src: Path, dst: Path) -> None:
    """复制文件；文本类强制 LF，避免 Android 上执行失败。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    raw = src.read_bytes()
    if src.suffix.lower() in LF_SUFFIXES:
        raw = raw.replace(b"\r\n", b"\n")
    dst.write_bytes(raw)


def stage_files(stage: Path) -> list[str]:
    """把模块文件与程序本体铺到一个临时目录，返回相对路径清单。"""
    copied: list[str] = []

    # 1) 模块脚本与 WebUI
    for rel in MODULE_FILES:
        src = MODULE_DIR / rel
        if not src.exists():
            raise SystemExit(f"缺少模块文件: {rel}")
        copy_as_lf(src, stage / rel)
        copied.append(rel)

    # 2) 程序本体：app/ 包（排除 __pycache__）
    for path in sorted(APP_DIR.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.suffix not in APP_SUFFIXES:
            continue
        rel = path.relative_to(ROOT)              # app/main.py
        copy_as_lf(path, stage / "app" / rel)     # app/app/main.py
        copied.append("app/" + rel.as_posix())

    # 3) 启动器与依赖清单
    for name in APP_EXTRA:
        src = ROOT / name
        if not src.exists():
            raise SystemExit(f"缺少项目文件: {name}")
        copy_as_lf(src, stage / "app" / name)
        copied.append("app/" + name)

    return copied


# --------------------------------------------------------------------- 校验

def verify(zip_path: Path) -> bool:
    """检查成品包：必备文件、换行、缓存残留、module.prop 字段。"""
    problems: list[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())

        for rel in MODULE_FILES:
            if rel not in names:
                problems.append(f"缺少 {rel}")
        for rel in ("app/run.py", "app/requirements.txt", "app/app/main.py", "app/app/config.py"):
            if rel not in names:
                problems.append(f"缺少 {rel}")

        for name in names:
            if "__pycache__" in name or name.endswith(".pyc"):
                problems.append(f"包含缓存文件 {name}")
            if name.endswith((".sh", ".prop")) and b"\r\n" in zf.read(name):
                problems.append(f"换行不是 LF: {name}")

        prop = read_prop_from(zf, "module.prop")
        for key in ("id", "name", "version", "versionCode", "author"):
            if not prop.get(key):
                problems.append(f"module.prop 缺少 {key}")

        # 模块目录名由 id 决定，必须稳定（换 id 会被当成另一个模块）
        if prop.get("id") != "badminton_booker":
            problems.append(f"module.prop 的 id 变了: {prop.get('id')!r}")

    if problems:
        print("\n[校验失败]")
        for item in problems:
            print("  -", item)
        return False

    print("[校验通过] 必备文件齐全、无缓存、换行正确")
    return True


def read_prop_from(zf: zipfile.ZipFile, name: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in zf.read(name).decode("utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            data[key.strip()] = value.strip()
    return data


# --------------------------------------------------------------------- 主流程

def main() -> int:
    parser = argparse.ArgumentParser(description="打包 KernelSU 模块")
    parser.add_argument("--out", default=str(ROOT / "dist"), help="输出目录，默认 dist/")
    parser.add_argument("--version", default="", help="覆盖包名里的版本号（不改文件）")
    parser.add_argument("--verify-only", action="store_true", help="只校验 dist 里最新的包")
    args = parser.parse_args()

    out_dir = Path(args.out).expanduser().resolve()

    # ---------------- 只校验
    if args.verify_only:
        existing = sorted(out_dir.glob("badminton-booker-ksu-*.zip"))
        if not existing:
            print(f"{out_dir} 下没有找到模块包")
            return 1
        target = existing[-1]
        print(f"校验 {target.name} ({target.stat().st_size / 1024:.1f} KB)")
        return 0 if verify(target) else 1

    # ---------------- 读取版本
    prop = read_prop(MODULE_DIR / "module.prop")
    version = args.version or prop.get("version", "").lstrip("v") or "0.0.0"
    if not version:
        print("module.prop 里没有 version")
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"badminton-booker-ksu-v{version.lstrip('v')}.zip"

    # ---------------- 铺文件
    stage = Path(tempfile.mkdtemp(prefix="ksu-stage-"))
    try:
        copied = stage_files(stage)

        # ---------------- 压缩（先写临时名，通过校验再改名，避免留下半个包）
        tmp_zip = zip_path.with_suffix(".zip.tmp")
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(stage).as_posix())
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    if not verify(tmp_zip):
        tmp_zip.unlink(missing_ok=True)
        return 1

    tmp_zip.replace(zip_path)

    # ---------------- 汇总
    size_kb = zip_path.stat().st_size / 1024
    print(f"\n模块包: {zip_path}")
    print(f"版本  : {prop.get('name', '?')} {prop.get('version', '')} "
          f"(versionCode={prop.get('versionCode', '?')})")
    print(f"内容  : {len(copied)} 个文件 / {size_kb:.1f} KB")
    print("\n下一步：把这个 zip 传到手机 → KernelSU 管理器 → 模块 → 从本地安装 → 重启")
    return 0


if __name__ == "__main__":
    sys.exit(main())
