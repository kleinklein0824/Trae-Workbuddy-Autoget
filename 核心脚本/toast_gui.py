#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯卡片悬浮弹窗：Edge 无头渲染卡片 PNG → Pillow 抠透明 → tkinter 透明窗口贴图 + 流光描边

卡片本体由浏览器引擎按 toast.html 组件逐像素渲染；白底经连通填充转透明（卡片内部
白色内容不受影响），圆角外全透明、无边框无衬底。
降级链：PNG 贴图 → notify.ps1 系统 Toast；绝不影响签到主流程。
"""
import ctypes
import json
import math
import subprocess
import sys
import tempfile
import time
import os

# 模板（toast.html）从「资源目录」取：exe 模式下在 PyInstaller 解包目录里；
# 输出（notify_card.png 等）写到「数据根目录」的 logs/ —— 不能写进包内目录，
# 那既不该被写、exe 装在只读位置时还会直接失败。
try:
    import runtime as _rt
    ROOT = _rt.data_root()
    _TPL_DIR = _rt.bundle_dir()
except Exception:  # noqa: BLE001  runtime 缺失时退回旧行为
    ROOT = os.path.dirname(os.path.abspath(__file__))
    _TPL_DIR = ROOT

HTML = os.path.join(_TPL_DIR, "toast.html")
# 模板没打进包时兜底到脚本同级（源码模式 / 开发调试）
if not os.path.exists(HTML):
    HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "toast.html")
LOG_DIR = os.path.join(ROOT, "logs")

KEY = "#010204"          # 透明色键
RAD = 32                 # 卡片圆角（与 CSS rounded-32px 一致）
CARD_W = 423             # 420 内容 + 1.5px*2 流光内衬
PALETTE = [(56, 189, 248), (129, 140, 248), (192, 132, 252), (244, 114, 182)]  # 参考案例原版流光四色
BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def _lerp(a, b, t):
    return int(a + (b - a) * t)


def _mix(c1, c2, t):
    return (_lerp(c1[0], c2[0], t), _lerp(c1[1], c2[1], t), _lerp(c1[2], c2[2], t))


def _build_html(payload, extra_css=""):
    with open(HTML, encoding="utf-8") as f:
        tpl = f.read()
    js = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    inject = "<script>window.__PAYLOAD__ = " + js + ";</script>"
    if extra_css:
        inject += "<style>" + extra_css + "</style>"
    return tpl.replace("</head>", inject + "\n</head>")


def _white_to_alpha(png_path):
    """边缘连通的白底 → 透明（卡片内部白色内容不连通外域，不受影响）。"""
    from PIL import Image, ImageDraw
    im = Image.open(png_path).convert("RGBA")
    seeds = [(0, 0), (im.width - 1, 0), (0, im.height - 1), (im.width - 1, im.height - 1),
             (im.width // 2, 0), (im.width // 2, im.height - 1),
             (0, im.height // 2), (im.width - 1, im.height // 2)]
    for s in seeds:
        try:
            ImageDraw.floodfill(im, s, (0, 0, 0, 0), thresh=25)
        except Exception:  # noqa: BLE001
            pass
    im.save(png_path)


def _render_png(payload, png_path):
    """无头浏览器渲染卡片 → 抠透明 PNG。成功返回 True。"""
    H = 250 + 30 * len(payload.get("rows", []))          # 纵向留裕量，卡片居中
    css = (f"html,body{{width:{CARD_W}px;height:{H}px}}\n"
           f"body{{background:#0b0c10}}\n"                # 参考案例页面底色
           f".stage{{left:0;top:0;transform:none;width:{CARD_W}px;animation:none}}\n"
           f".frame{{box-shadow:none}}\n"
           # 双层流光保留（锐利层供边框、模糊层供透光）；薄垫底 + 增强 saturate 还原参考案例的鲜艳彩光
           f".card{{background:radial-gradient(120% 120% at 50% 0%, rgba(255,255,255,.15) 0%,"
           f" rgba(255,255,255,.03) 60%, rgba(13,14,22,.78) 100%), rgba(20,23,33,.30) !important;"
           f" backdrop-filter:blur(32px) saturate(190%) !important}}")
    tmp_html = os.path.join(LOG_DIR, "notify_last.html")
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(tmp_html, "w", encoding="utf-8") as f:
        f.write(_build_html(payload, css))
    uri = "file:///" + tmp_html.replace("\\", "/").replace(" ", "%20")
    # 每次渲染用一个**全新的临时 profile**，而不是"固定目录 + 先递归删干净"：
    #   · 递归删除正好跑在「出弹窗」的关键路径上 —— 它一旦失败，弹窗就没了，
    #     而且失败原因（权限、锁、被安全软件拦）跟弹窗本身毫无关系；
    #   · 固定目录还会跟上一轮残留的 Chromium 抢锁；
    #   · 交给 Windows 的 %TEMP% 自行回收，比在日志目录里反复建删更干净。
    # 结论：**渲染路径上不做任何删除**。
    profile = tempfile.mkdtemp(prefix="tb_toast_profile_")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for exe in BROWSERS:
        if not os.path.exists(exe):
            continue
        try:
            subprocess.run(
                [exe, "--headless", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                 "--default-background-color=00000000", "--force-color-profile=srgb",
                 f"--screenshot={png_path}", f"--window-size={CARD_W},{H}",
                 f"--user-data-dir={profile}", uri],
                creationflags=flags, timeout=30,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            continue
        if os.path.exists(png_path) and os.path.getsize(png_path) > 1000:
            _white_to_alpha(png_path)
            return True
    return False


def _bounds(img, w, h):
    """扫描 PNG 非透明区域包围盒（卡片精确位置）。"""
    my, mx = h // 2, w // 2

    def opaque(x, y):
        try:
            return not img.transparency_get(x, y)
        except Exception:  # noqa: BLE001
            return True

    left = next((x for x in range(0, 80) if opaque(x, my)), 0)
    right = next((x for x in range(w - 1, w - 81, -1) if opaque(x, my)), w - 1)
    top = next((y for y in range(0, 90) if opaque(mx, y)), 0)
    bottom = next((y for y in range(h - 1, h - 91, -1) if opaque(mx, y)), h - 1)
    return left, top, right, bottom


def show_png(p):
    import tkinter as tk

    try:  # DPI 感知：tk 逻辑坐标 = 物理像素，保证右下角定位精确
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        pass

    png = os.path.join(LOG_DIR, "notify_card.png")
    if not _render_png(p, png):
        raise RuntimeError("headless screenshot failed")
    sw, sh = ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=KEY)
    try:
        root.attributes("-transparentcolor", KEY)
    except Exception:  # noqa: BLE001
        pass
    img = tk.PhotoImage(file=png)
    W, H = img.width(), img.height()
    tx, ty = sw - W - 16, sh - H - 60
    root.geometry(f"{W}x{H}+{tx}+{ty}")
    root.attributes("-alpha", 0.0)

    cv = tk.Canvas(root, width=W, height=H, bg=KEY, highlightthickness=0, bd=0)
    cv.pack()
    cv.create_image(0, 0, image=img, anchor="nw")
    root._img = img

    # ---- 流动彩虹描边（沿卡片实测包围盒） ----
    x0, y0, x1, y1 = _bounds(img, W, H)
    r = RAD
    segs = []

    def add_arc(bx0, by0, bx1, by1, start, extent):
        for i in range(3):
            sid = cv.create_arc(bx0, by0, bx1, by1, start=start + i * extent / 3,
                                extent=extent / 3, style="arc", outline="#fff", width=2)
            segs.append((sid, True))

    def add_line(ax1, ay1, ax2, ay2, parts):
        for i in range(parts):
            t0, t1 = i / parts, (i + 1) / parts
            sid = cv.create_line(_lerp(ax1, ax2, t0), _lerp(ay1, ay2, t0),
                                 _lerp(ax1, ax2, t1), _lerp(ay1, ay2, t1),
                                 fill="#fff", width=2)
            segs.append((sid, False))

    add_arc(x0, y0, x0 + 2 * r, y0 + 2 * r, 90, 90)
    add_line(x0 + r, y0, x1 - r, y0, 6)
    add_arc(x1 - 2 * r, y0, x1, y0 + 2 * r, 0, 90)
    add_line(x1, y0 + r, x1, y1 - r, 6)
    add_arc(x1 - 2 * r, y1 - 2 * r, x1, y1, 270, 90)
    add_line(x0 + r, y1, x1 - r, y1, 6)
    add_arc(x0, y1 - 2 * r, x0 + 2 * r, y1, 180, 90)
    add_line(x0, y0 + r, x0, y1 - r, 6)

    state = {"phase": 0.0, "closing": False, "alive": True}

    def tick():
        if not state["alive"] or state["closing"]:
            return
        state["phase"] = (state["phase"] + 0.5) % len(PALETTE)
        m = len(segs)
        for i, (sid, is_arc) in enumerate(segs):
            pos = (i / m * len(PALETTE) + state["phase"]) % len(PALETTE)
            j = int(pos)
            c = _mix(PALETTE[j], PALETTE[(j + 1) % len(PALETTE)], pos - j)
            try:
                cv.itemconfig(sid, **({"outline": "#%02x%02x%02x" % c} if is_arc
                                     else {"fill": "#%02x%02x%02x" % c}))
            except Exception:  # noqa: BLE001
                pass
        root.after(66, tick)

    def close():
        if state["closing"]:
            return
        state["closing"] = True
        for i in range(11):
            try:
                root.attributes("-alpha", 1 - i / 10)
                root.update()
            except Exception:  # noqa: BLE001
                break
            root.after(26)
        state["alive"] = False
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass

    cv.bind("<Button-1>", lambda e: close())
    root.bind("<Escape>", lambda e: close())
    # ★ 显示时长 12s（原 7s）：7s 太短 —— 2026-09-19 用户"走神"就没看到
    #   第二次「补跑完成」弹窗，事后无法确认它到底弹没弹。与 toast.html 对齐。
    root.after(12000, close)
    root.after(12000, lambda: state["alive"] and close())
    for i in range(8):                                   # 淡入
        root.attributes("-alpha", (i + 1) / 8)
        root.update()
        root.after(24)
    tick()
    root.mainloop()


def main():
    try:
        payload = json.loads(sys.argv[1])
    except Exception:  # noqa: BLE001
        return
    try:
        show_png(payload)
    except Exception as e:  # noqa: BLE001
        # 失败只记日志，不再触发系统通知（避免出现第二个弹窗）
        try:
            import traceback
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(os.path.join(LOG_DIR, "toast_err.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%F %T')} {traceback.format_exc() or e!r}\n")
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
