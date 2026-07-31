@echo off
rem =============================================================
rem 股票浮窗 Windows 版 一键打包脚本
rem 在 Windows 机器上双击本文件, 即可生成 发布\股票浮窗.exe
rem 要求: Windows 10/11, 可联网(自动安装 PyInstaller)
rem 说明:
rem   - 自动查找 Python 3.11+ (需含 tkinter, python.org 官方版自带)
rem   - 产物为单文件 exe, 首次运行在 exe 同级生成 config.toml /
rem     stocks.toml (可编辑, 热重载 2s 生效); 信号 CSV 也落在同级
rem =============================================================
chcp 65001 >nul
title 股票浮窗 Windows 版打包
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo ============================================
echo   股票浮窗 Windows 版 一键打包
echo ============================================
echo.

rem ---- 1) 查找 Python 3.11+ (含 tkinter) ----
set "PY="
py -c "import sys,tkinter" >nul 2>nul
if not errorlevel 1 (
  for /f "delims=" %%v in ('py -c "import sys;print(1 if sys.version_info>=(3,11) else 0)" 2^>nul') do if "%%v"=="1" set "PY=py"
)
if not defined PY (
  for %%C in (python3.13 python3.12 python3.11 python) do (
    where %%C >nul 2>nul
    if not errorlevel 1 (
      %%C -c "import sys,tkinter" >nul 2>nul
      if not errorlevel 1 (
        for /f "delims=" %%v in ('%%C -c "import sys;print(1 if sys.version_info>=(3,11) else 0)" 2^>nul') do if "%%v"=="1" set "PY=%%C" && goto :found
      )
    )
  )
)
echo [错误] 未找到 Python 3.11+ (需含 tkinter)。
echo 请安装 python.org 官方版并勾选 "Add python.exe to PATH":
echo   https://www.python.org/downloads/
pause
exit /b 1

:found
echo [1/5] 使用解释器: %PY%

rem ---- 2) 生成图标 ico ----
echo [2/5] 生成图标 股票浮窗.ico ...
%PY% -c "import PIL" >nul 2>nul || %PY% -m pip install pillow
%PY% make_icon.py
if errorlevel 1 (
  echo [错误] 图标生成失败, 请检查 pillow 安装。
  pause
  exit /b 1
)

rem ---- 3) 打包模板: 本地真实配置优先, 否则用仓库 example ----
for %%F in (config.toml stocks.toml) do (
  if not exist "%%F" (
    if exist "..\%%F.example" (
      copy /Y "..\%%F.example" "%%F" >nul
      echo [提示] 使用模板 ..\%%F.example 作为打包配置
    )
  )
)

rem ---- 3.5) 同步最新主程序: 打包源 = 仓库根 stock_float.py(避免双份源码漂移) ----
if exist "..\stock_float.py" (
  copy /Y "..\stock_float.py" "stock_float.py" >nul
  echo [提示] 已同步 ..\stock_float.py -^> stock_float.py
)
if not exist "stock_float.py" (
  echo [错误] 缺少 stock_float.py(boot.py 依赖它), 请从仓库根目录复制后重试。
  pause
  exit /b 1
)

rem ---- 4) 安装 PyInstaller (官方源失败则用清华镜像) ----
echo [4/4] 检查 / 安装 PyInstaller ...
%PY% -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
  %PY% -m pip install pyinstaller || ^
  %PY% -m pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
  if errorlevel 1 (
    echo [错误] PyInstaller 安装失败, 请检查网络后重试。
    pause
    exit /b 1
  )
)

rem ---- 5) 打包单文件 exe ----
echo [5/5] PyInstaller 打包 (单文件, 无控制台) ...
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name 股票浮窗 ^
  --icon 股票浮窗.ico ^
  --add-data "config.toml;." ^
  --add-data "stocks.toml;." ^
  boot.py
if errorlevel 1 (
  echo [错误] 打包失败, 请查看上方日志。
  pause
  exit /b 1
)

rem ---- 发布 ----
echo.
echo 发布到 ..\发布\ ...
if not exist "..\发布" mkdir "..\发布"
copy /Y "dist\股票浮窗.exe" "..\发布\股票浮窗.exe" >nul
if errorlevel 1 (
  echo [错误] 复制失败。
  pause
  exit /b 1
)

echo.
echo ============================================
echo   ✔ 构建完成: ..\发布\股票浮窗.exe
echo   双击即可运行; config.toml / stocks.toml /
echo   信号 CSV 自动出现在 exe 同级目录。
echo ============================================
pause
