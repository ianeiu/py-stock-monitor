@echo off
chcp 65001 >nul 2>&1
rem ============================================================
rem  启动浮窗.bat  —— Windows 双击启动股票浮窗
rem  自动查找 Python 3.11+ (需支持 tkinter); 找不到会提示安装。
rem  macOS 专用通知在 Windows 上会自动跳过(可正常盯盘)。
rem ============================================================
setlocal

set "SCRIPT_DIR=%~dp0"
set "TARGET=%SCRIPT_DIR%stock_float.py"

rem 1) 在 PATH 中查找可用的 Python (要求 >= 3.11 且支持 tkinter)
set "PY="
call :findpy python
if not defined PY call :findpy py
if not defined PY call :findpy python3

if not defined PY (
    echo [错误] 未找到 Python 3.11+。请安装 Python 3.11 或更高版本后重试。
    echo         下载: https://www.python.org/downloads/  (安装时勾选 "Add python.exe to PATH")
    pause
    exit /b 1
)

:found
echo 使用 Python: %PY%
echo 启动浮窗: %TARGET%
"%PY%" "%TARGET%"
if %errorlevel% neq 0 (
    echo.
    echo [浮窗已退出] 退出码 %errorlevel%
    pause
)
endlocal
goto :eof

rem ---------- 子程序: 校验某解释器是否 >= 3.11 且支持 tkinter ----------
:findpy
"%1" -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if errorlevel 1 exit /b 1
set "PY=%1"
exit /b 0
