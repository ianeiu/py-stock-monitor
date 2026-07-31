#!/bin/bash
# =============================================================
# 股票浮窗 macOS App 一键构建脚本
# 用法: ./build_app.sh
# 产物: ../发布/股票浮窗.app  (双击即可运行, 无需安装 Python)
# 说明:
#   - 自动选择带 tkinter 的 Python (优先 WorkBuddy managed 3.13.x,
#     回退到系统 python3.13/3.12/3.11, 需含 tkinter)
#   - 打包模板: 优先使用本目录 config.toml / stocks.toml (个人自选股),
#     不存在则回退到仓库根目录的 config.toml.example / stocks.toml.example
#   - 首次运行 app 时, 会在 .app 同级自动生成 config.toml / stocks.toml
#     (从内置模板复制, 用户可编辑, 热重载 2s 生效)
#   - 信号 CSV(signals_*.csv) 同样落在 .app 同级目录
# =============================================================
set -euo pipefail
cd "$(dirname "$0")"

# ---- 选择 Python: 优先已装 PyInstaller 的 venv, 其次带 tkinter 的解释器 ----
PY=""
for p in "$HOME/.workbuddy/binaries/python/envs/default/bin/python" \
         "$HOME"/.workbuddy/binaries/python/versions/*/bin/python3; do
  [ -x "$p" ] || continue
  if "$p" -c "import tkinter, PyInstaller" 2>/dev/null; then
    PY="$p"; break
  fi
done
if [ -z "$PY" ]; then
  for d in "$HOME"/.workbuddy/binaries/python/versions/*/; do
    [ -d "$d" ] || continue
    if "${d}bin/python3" -c "import tkinter" 2>/dev/null; then
      PY="${d}bin/python3"
      break
    fi
  done
fi
if [ -z "$PY" ]; then
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c "import tkinter" 2>/dev/null; then
      PY="$(command -v "$c")"
      break
    fi
  done
fi
if [ -z "$PY" ]; then
  echo "✗ 未找到带 tkinter 的 Python 3.11+, 无法打包。请安装: https://www.python.org/downloads/"
  exit 1
fi
echo "▶ 使用解释器: $PY"
if ! "$PY" -c "import PyInstaller" 2>/dev/null; then
  echo "▶ 安装 PyInstaller ..."
  "$PY" -m pip install pyinstaller -i https://mirrors.aliyun.com/pypi/simple/ \
    || "$PY" -m pip install pyinstaller
fi

# ---- 打包模板: 本地真实配置优先, 否则用仓库 example ----
for f in config.toml stocks.toml; do
  if [ ! -f "$f" ] && [ -f "../$f.example" ]; then
    cp "../$f.example" "$f"
    echo "▶ 使用模板 ../$f.example 作为打包配置"
  fi
done

# ---- 同步最新主程序: 打包源 = 仓库根 stock_float.py(避免双份源码漂移) ----
if [ -f "../stock_float.py" ]; then
  cp "../stock_float.py" stock_float.py
  echo "▶ 已同步 ../stock_float.py -> stock_float.py"
fi
if [ ! -f "stock_float.py" ]; then
  echo "✗ 缺少 stock_float.py(boot.py 依赖它), 请从仓库根目录复制后重试"
  exit 1
fi

APP_NAME=股票浮窗

echo "▶ 生成图标 icns ..."
"$PY" make_icon.py

echo "▶ PyInstaller 打包 ..."
"$PY" -m PyInstaller --noconfirm --clean \
    --windowed \
    --name StockFloat \
    --icon 股票浮窗.icns \
    --add-data config.toml:. \
    --add-data stocks.toml:. \
    boot.py

echo "▶ 调整 Info.plist (中文显示名 / 版本 / 高分辨率) ..."
PLIST=dist/StockFloat.app/Contents/Info.plist
/usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string $APP_NAME" "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName $APP_NAME" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleName string $APP_NAME" "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :CFBundleName $APP_NAME" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string 1.0.0" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleVersion string 1" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :NSHighResolutionCapable bool true" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string 12.0" "$PLIST" 2>/dev/null || true

echo "▶ 发布到 ../发布/ ..."
rm -rf "../发布/$APP_NAME.app"
mkdir -p ../发布
cp -R dist/StockFloat.app "../发布/$APP_NAME.app"

echo ""
echo "✔ 构建完成: ../发布/$APP_NAME.app"
echo "  双击即可运行; 配置文件与信号 CSV 自动出现在 app 同级目录。"
