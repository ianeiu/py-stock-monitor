#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""股票浮窗 跨平台引导入口 (macOS .app / Windows .exe)。

打包方式:
    macOS (build_app.sh):
        PyInstaller --windowed --name StockFloat --icon 股票浮窗.icns \
            --add-data config.toml:. --add-data stocks.toml:. boot.py
    Windows (build_windows.bat):
        pyinstaller --noconfirm --clean --onefile --windowed \
            --name 股票浮窗 --icon 股票浮窗.ico \
            --add-data config.toml;. --add-data stocks.toml;. boot.py

说明:
    - 显式 import stock_float(与 boot.py 同目录), 让 PyInstaller 静态分析收集引擎
      及其全部依赖(tkinter / urllib / zoneinfo ...)。
    - 运行时路径约定(延续原项目的"配置文件在程序同级"):
        macOS:  .app 所在目录 = 发布目录
        Windows: .exe 所在目录 = 发布目录
      首次启动把内置的 config.toml / stocks.toml 模板复制到发布目录(已有则不覆盖),
      用户可随时编辑, 浮窗热重载(2s 轮询 mtime)照常生效; 信号 CSV 也落在发布目录。
"""
import os
import shutil
import sys

import stock_float  # noqa: E402  (与 boot.py 同目录, PyInstaller 会收集)


def app_dir() -> str:
    """定位发布目录(配置 / CSV 落盘处)。

    macOS 打包后: <发布目录>/股票浮窗.app/Contents/MacOS/StockFloat
    Windows 打包后: <发布目录>/股票浮窗.exe
    """
    exe = os.path.abspath(sys.executable)
    if sys.platform == "darwin":
        parts = exe.split(os.sep)
        for i, p in enumerate(parts):
            if p.endswith(".app"):
                return os.sep.join(parts[:i])
        # 兜底: 从 Contents/MacOS 上溯 4 级
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(exe))))
    # Windows / Linux: exe 所在目录
    return os.path.dirname(exe)


def bundled(name: str) -> str:
    """程序内部捆绑资源路径(macOS one-dir 为 Resources/_internal; Windows onefile 为临时解压目录)。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def main() -> None:
    d = app_dir()

    # 1) 首次运行: 把默认配置模板复制到发布目录(已有则不覆盖, 用户可编辑+热重载)
    for f in ("config.toml", "stocks.toml"):
        dest = os.path.join(d, f)
        if not os.path.exists(dest):
            shutil.copy(bundled(f), dest)

    # 2) 把配置 / 落盘目录重定向到发布目录
    #    (模块内对 SCRIPT_DIR / SIGNAL_CSV 的引用均为运行时查全局, 改属性即生效)
    stock_float.SCRIPT_DIR = d
    stock_float.SIGNAL_CSV = os.path.join(d, "signals.csv")

    # 3) 进入主循环(argparse 使用 sys.argv, 双击时即程序名, 正常解析)
    stock_float.main()


if __name__ == "__main__":
    main()
