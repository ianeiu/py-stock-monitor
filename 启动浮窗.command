#!/bin/bash
# 股票浮窗 启动器 —— 双击此文件即可运行 (macOS)
# 作用: cd 到脚本所在目录, 自动查找 Python >= 3.11 (含 tkinter) 启动 stock_float.py

# 静音 macOS 系统 Tk 弃用警告 (仅在使用系统 Tk 时出现)
export TK_SILENCE_DEPRECATION=1

# 切到脚本所在目录, 保证能找到 stock_float.py
cd "$(dirname "$0")" || exit 1

# 检查某个解释器是否满足要求: Python >= 3.11 且能 import tkinter
check_python() {
  local p="$1"
  if [ ! -x "$p" ]; then
    p="$(command -v "$1" 2>/dev/null)"
  fi
  [ -x "$p" ] || return 1
  "$p" -c "import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>/dev/null
}

PY=""

# 1) 优先 WorkBuddy managed python (跨用户: 用 $HOME, 不写死用户名)
for d in "$HOME"/.workbuddy/binaries/python/versions/*/; do
  [ -d "$d" ] || continue
  if check_python "${d}bin/python3"; then
    PY="${d}bin/python3"
    break
  fi
done

# 2) 回退到系统/环境 python (python3.13 / 3.12 / 3.11 / python3)
if [ -z "$PY" ]; then
  for c in python3.13 python3.12 python3.11 python3; do
    if check_python "$c"; then
      PY="$(command -v "$c" 2>/dev/null)"
      break
    fi
  done
fi

if [ -z "$PY" ]; then
  echo "✗ 未找到 Python >= 3.11 (需要含 tkinter), 无法启动。"
  echo "  请安装 Python 3.11+ 后重试: https://www.python.org/downloads/"
  read -r _ 2>/dev/null
  exit 1
fi

echo "▶ 使用解释器: $PY"
echo "▶ 启动股票浮窗 (浮动窗口, 实时刷新; 拖顶部条可移动, 🗑/右键删除自选)"
# exec 替换 bash 进程(仅剩 python3 一层), 关窗不再弹 "终止进程?" 确认框
exec "$PY" stock_float.py
