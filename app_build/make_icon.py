#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成股票浮窗图标: 1024x1024 PNG -> .icns(macOS) / .ico(Windows)

设计: 深蓝渐变背景 + 居中白色粗体 "S" 字母(无任何股市元素)。
用法: python3 make_icon.py
依赖: pillow(仅构建期使用, 不进入 App)
"""
import os
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# ---- 背景: 深蓝渐变圆角方块 ----
top, bottom = (35, 46, 82, 255), (16, 21, 40, 255)
for y in range(S):
    t = y / S
    r = int(top[0] + (bottom[0] - top[0]) * t)
    g = int(top[1] + (bottom[1] - top[1]) * t)
    b = int(top[2] + (bottom[2] - top[2]) * t)
    d.line([0, y, S, y], fill=(r, g, b, 255))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=230, fill=255)
bg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
bg.paste(img, (0, 0), mask)
img = bg
d = ImageDraw.Draw(img)

# ---- 居中白色粗体 "S" ----
if sys.platform == "win32":
    FONT_CANDIDATES = [
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\seguisb.ttf",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\arial.ttf",
    ]
else:
    FONT_CANDIDATES = [
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
font = None
for f in FONT_CANDIDATES:
    if os.path.exists(f):
        try:
            font = ImageFont.truetype(f, 660)
            break
        except Exception:
            continue
if font is None:
    raise RuntimeError("未找到可用的系统字体")

bbox = d.textbbox((0, 0), "S", font=font)
w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
d.text(((S - w) / 2 - bbox[0], (S - h) / 2 - bbox[1]), "S",
       font=font, fill=(250, 251, 255, 255))

out_png = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon_1024.png")
img.save(out_png)
print("PNG ->", out_png)


def build_icns(png_path: str) -> str:
    work = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".icon.iconset")
    os.makedirs(work, exist_ok=True)
    sizes = [(16, "icon_16x16"), (32, "icon_16x16@2x"), (32, "icon_32x32"),
             (64, "icon_32x32@2x"), (128, "icon_128x128"), (256, "icon_128x128@2x"),
             (256, "icon_256x256"), (512, "icon_256x256@2x"), (512, "icon_512x512"),
             (1024, "icon_512x512@2x")]
    src = Image.open(png_path)
    for px, name in sizes:
        im = src.resize((px, px), Image.LANCZOS)
        im.save(os.path.join(work, name + ".png"))
    icns = os.path.join(os.path.dirname(os.path.abspath(__file__)), "股票浮窗.icns")
    subprocess.run(["iconutil", "-c", "icns", work, "-o", icns], check=True)
    # 清理临时目录(失败可忽略, 不影响产物)
    try:
        subprocess.run(["rm", "-rf", work], check=False)
    except Exception:
        pass
    return icns


if sys.platform == "darwin" and shutil.which("iconutil"):
    icns = build_icns(out_png)
    print("ICNS ->", icns)
else:
    print("跳过 ICNS(macOS only)")

ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "股票浮窗.ico")
Image.open(out_png).save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("ICO ->", ico)
