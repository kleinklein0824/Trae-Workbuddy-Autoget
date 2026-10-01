#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`_shot_util` 的回归探针：证明「截图坐标必须乘 DPI 缩放比」这件事。

背景（2026-09-18 踩的坑）
-------------------------
本机真实屏幕 1920x1080、系统缩放 125%，而 Tk 进程
`GetProcessDpiAwareness = UNAWARE` → 被 Windows **DPI 虚拟化**：
`winfo_screenwidth/rootx/width` 全是**虚拟坐标**（1536x864 体系），
而 `ImageGrab.grab(bbox=...)` 走 GDI 按**物理像素**裁剪。

于是 `grab(bbox=(winfo_rootx, winfo_rooty, +width, +height))`
只抓到窗口**左上 1/1.25 = 80%** 的区域，右侧 20% 落在图外 ——
截出来的图里文字"到右边缘就断了"，**看起来就像界面被裁**。
而所有 Tk 层断言（`reqwidth <= width`、`reqheight <= height`）都是绿的。
白查了好几轮。

★ 本探针自带正对照
------------------
第 2 节**故意**用错误方式抓一次，并断言「它确实比正确方式窄」。
如果哪天环境变了（缩放变 100%），正对照会报 SKIP 而不是 FAIL ——
因为那时"错误方式"其实是对的，判据不适用（前置条件不满足 ≠ 失败）。
"""
import os
import sys
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_DIR = os.path.dirname(HERE)
_CORE_DIR = os.path.join(os.path.dirname(GUI_DIR), "核心脚本")
_PROJ_ROOT = (_CORE_DIR if os.path.isfile(os.path.join(_CORE_DIR, "checkin.py"))
             else os.path.dirname(os.path.dirname(GUI_DIR)))
for _p in (HERE, _PROJ_ROOT, GUI_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

OK = FAIL = SKIP = 0


def check(label, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  OK   %s" % label)
    else:
        FAIL += 1
        print("  FAIL %s   %s" % (label, extra))


def skip(label, why):
    global SKIP
    SKIP += 1
    print("  SKIP %s   （%s）" % (label, why))


def settle(root, n=14):
    for _ in range(n):
        try:
            root.update_idletasks()
            root.update()
        except tk.TclError:
            return


def main():
    import _shot_util as SU
    from PIL import ImageGrab

    root = tk.Tk()
    root.title("shot-util-probe")
    root.geometry("640x300+60+60")
    # ★★ 必须 topmost：本探针第 5 节要**按像素找红色**，而 `ImageGrab.grab()`
    #    抓的是**屏幕**。2026-09-20 实测：窗口被浏览器压在下面，抓到的整张图
    #    是浏览器页面，**红像素 0 个**（Tk 层断言全真、只有像素是错的）。
    #    这**不是**放松判据 —— 是把「窗口是否可见」这个与被测对象无关的环境
    #    变量钉死；坐标映射要是错了（比如忘了乘 k），topmost 一样扫不到红色。
    root.attributes("-topmost", True)
    root.deiconify()
    root.lift()
    try:
        root.focus_force()
    except tk.TclError:
        pass
    settle(root, 20)

    print("== 1. 缩放比检测 ==")
    k = SU.dpi_scale(root)
    vw, vh = root.winfo_screenwidth(), root.winfo_screenheight()
    full = ImageGrab.grab(all_screens=True)
    print("     虚拟屏幕 %dx%d；全屏抓图（物理）%dx%d；缩放比 k=%.4f"
          % (vw, vh, full.width, full.height, k))
    check("dpi_scale() 与「全屏物理宽/虚拟屏宽」一致（%.4f）"
          % (full.width / float(vw)),
          abs(k - full.width / float(vw)) < 0.01)
    check("dpi_scale() 落在合理区间 [1.0, 4.0]",
          1.0 <= k <= 4.0, "k=%r" % k)

    print("\n== 2. 【正对照】不乘缩放比时，确实会漏掉右侧 ==")
    naive = ImageGrab.grab(bbox=(root.winfo_rootx(), root.winfo_rooty(),
                                 root.winfo_rootx() + root.winfo_width(),
                                 root.winfo_rooty() + root.winfo_height()))
    good = SU.grab_window(root, k)
    print("     错误方式（原始坐标）%dx%d；正确方式（×k）%dx%d"
          % (naive.width, naive.height, good.width, good.height))
    if abs(k - 1.0) < 0.02:
        skip("错误方式应比正确方式窄",
             "本机缩放为 1.0，两种方式等价，判据不适用")
    else:
        check("错误方式确实更窄（漏掉了右侧 %.0f%% 的宽度）"
              % ((1 - naive.width / float(good.width)) * 100),
              naive.width < good.width - 2,
              "naive=%d good=%d" % (naive.width, good.width))

    print("\n== 3. 正确方式的图宽 == 逻辑宽 × k ==")
    expect = int(root.winfo_width() * k)
    print("     逻辑宽 %d × k %.3f = %d；实测 %d"
          % (root.winfo_width(), k, expect, good.width))
    check("grab_window() 抓的是**完整**窗口", abs(good.width - expect) <= 2,
          "expect=%d got=%d" % (expect, good.width))

    print("\n== 4. 别被 winfo_fpixels / GetDpiForWindow 骗了 ==")
    fp = root.winfo_fpixels("1i")
    print("     winfo_fpixels('1i')=%.2f → 假缩放比 %.4f（真实 %.4f）"
          % (fp, fp / 96.0, k))
    check("winfo_fpixels('1i')/96 在虚拟化进程里会给出 1.0（所以不能用它）",
          abs(fp / 96.0 - 1.0) < 0.05 or abs(fp / 96.0 - k) < 0.05,
          "fp=%.2f k=%.3f" % (fp, k))

    print("\n== 5. to_image_xy 与 grab_window 自洽 ==")
    lab = tk.Label(root, text="anchor", bg="#ff0000", fg="#ffffff")
    lab.place(x=40, y=30, width=120, height=40)
    settle(root, 10)
    ix, iy = SU.to_image_xy(root, lab)
    print("     label Tk 位置 (%d,%d)；图内物理坐标 (%d,%d)"
          % (lab.winfo_rootx(), lab.winfo_rooty(), ix, iy))
    check("图内坐标 = (控件root - 窗口root) × k",
          ix == int((lab.winfo_rootx() - root.winfo_rootx()) * k)
          and iy == int((lab.winfo_rooty() - root.winfo_rooty()) * k))
    # ★ 必须在 label 放好**之后**重新抓图：第 2 节那张 `good` 是放 label 前抓的，
    #   里面根本没有这个 label（第一版就栽在这——拿旧图去判新控件）。
    #
    # ★★ 这一段依赖「屏幕不被遮挡」：`ImageGrab.grab()` 抓的是**屏幕**，
    #    被别的窗口压住时抓到的就是别人的像素 —— Tk 层断言全真、只有像素是错的。
    #    2026-09-20 实测：窗口被浏览器压住，抓到的整张图是浏览器页面，
    #    **红像素 0 个**。已在 main() 里把 root 设成 `-topmost`；这里再
    #    lift()+focus_force()，并保留**有界重试**（首帧可能还没画上）。
    #    ⚠️ 重试不会把断言变松 —— 坐标映射要是错了，重试一万次也扫不到红色。
    root.lift()
    try:
        root.focus_force()
    except tk.TclError:
        pass

    # ★ 扫描框留 ±2px 余量。实测（2026-09-20 本机 125%）：
    #     `to_image_xy` 给出 (50,37)，而红色真实 bbox = (50,39,199,87)
    #     → 水平 dx=0（映射**精确**），垂直 dy=2。
    #   原因是 `int(30 * 1.25) = 37` 落在 label 的**半个像素行**上（37.5），
    #   而 label 首个**完整**物理行是 39，贴着算就落在背景行上了。
    #   2px 容差不会掩盖真问题：漏乘 k 的话，x 方向在 x=40 处就差 10px，
    #   120 宽的 label 右端差 30px —— 照样扫不到。
    def _scan_red(img, x0, y0, pad=2, box=22):
        px = img.convert("RGB").load()
        x_lo, y_lo = max(0, x0 - pad), max(0, y0 - pad)
        for yy in range(y_lo, min(y_lo + box, img.height)):
            for xx in range(x_lo, min(x_lo + box, img.width)):
                r, g, b = px[xx, yy]
                if r > 200 and g < 80 and b < 80:
                    return True
        return False

    hit = False
    for attempt in range(6):
        if attempt:
            settle(root, 3)
        if _scan_red(SU.grab_window(root, k), ix, iy):
            hit = True
            break
    check("按 to_image_xy 定位到的像素确实是 label 的红色", hit)
    # 正对照：故意错位 200px 必须扫不到红色（否则说明扫描根本没在看颜色，
    # 或者窗口底色本身就是红的 —— 那样上面那条断言等于没判）。
    check("【正对照】错位 200px 扫不到红色（判据不是死的）",
          not _scan_red(SU.grab_window(root, k), ix + 200, iy))

    root.destroy()
    print("\n结果：%d OK / %d FAIL / %d SKIP" % (OK, FAIL, SKIP))
    sys.stdout.flush()
    os._exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    sys.exit(main())
