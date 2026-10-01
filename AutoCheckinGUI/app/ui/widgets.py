#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用 UI 组件：用 Tkinter Canvas 复刻参考项目的卡片化视觉语言。

参考项目用 Tailwind 类实现 card / chip / btn / stat 等原子组件，这里对应
实现同名 Python 组件：圆角卡片、徽标(chip)、按钮、进度条、统计卡、空状态、
模态框、开关、滚动容器。全部接受 theme 参数以支持亮/暗主题切换。
"""
import tkinter as tk
import weakref
from tkinter import font as tkfont
from tkinter import ttk


def alive(widget):
    """判断控件是否仍然存在。

    页面重建（切换主题 / 重新渲染）后，后台线程的 after 回调可能落到已销毁的
    控件上，Tkinter 会抛 TclError: invalid command name。统一用它做守卫。
    """
    try:
        return bool(widget.winfo_exists())
    except Exception:  # noqa: BLE001
        return False


# ---------------- 基础绘制工具 ----------------

def auto_wrap(label, min_wrap=120, slack=4):
    """让 Label 的 wraplength 跟随自身宽度 —— 长文本不再被卡片右边缘裁掉。

    tk.Label **不会自动换行**：wraplength=0（默认）时，文本按整行渲染，
    超出的部分被父容器直接裁掉，界面上表现成"文字显示不全"，
    而且不会有任何报错。凡是可能出现长路径 / 长句 / 长 ID 的标签，
    都应该套一层这个。

    带宽度变化守卫：改 wraplength 会改变标签**高度**并再次触发 <Configure>，
    不判断"宽度是否真的变了"就会来回抖动。
    """
    state = {"w": 0}

    def _apply(e=None):
        try:
            w = int(e.width) if e is not None else label.winfo_width()
        except (AttributeError, TypeError, ValueError, tk.TclError):
            return
        if w <= 40 or abs(w - state["w"]) < 8:
            return
        state["w"] = w
        try:
            label.configure(wraplength=max(min_wrap, w - slack))
        except tk.TclError:
            pass

    try:
        label.bind("<Configure>", _apply, add="+")
    except tk.TclError:
        pass
    return label


def round_rect(cv, x1, y1, x2, y2, r, **kw):
    """在 Canvas 上画圆角矩形（用平滑多边形近似，避免逐像素绘制开销）。"""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return cv.create_polygon(pts, smooth=True, **kw)


class Card(tk.Frame):
    """圆角卡片：白色底 + 1px 边框 + 可选顶部强调条。

    实现要点（踩过的坑）：
    * `tk.Canvas` 默认尺寸是 378x265，必须显式覆盖，否则 `winfo_reqwidth`
      会把整页撑出可视区（表现为第 4 张统计卡被裁掉）。
    * 但也不能把 height 固定成 1：那样内层 body 永远拿不到高度，
      卡片会塌成一条线。正确做法是「宽度由外部决定，高度跟随内容」——
      即 canvas 的 height 用 body 的 reqheight 回写，并且要主动触发一次。
    """

    def __init__(self, master, theme, accent=None, padding=14, radius=12, **kw):
        super().__init__(master, bg=theme.bg, highlightthickness=0, bd=0, **kw)
        self.theme = theme
        self.radius = radius
        self.accent = accent
        self.padding = padding
        self._syncing = False
        self._last_size = None
        self._redraw_id = None
        self._sync_id = None
        self._cv = tk.Canvas(self, bg=theme.bg, highlightthickness=0, bd=0,
                             width=1, height=1)
        self._cv.pack(fill="both", expand=True)
        self.body = tk.Frame(self._cv, bg=theme.bg_soft)
        self._win = self._cv.create_window(padding, padding, anchor="nw",
                                           window=self.body)
        self._cv.bind("<Configure>", self._on_configure)
        self.body.bind("<Configure>", self._on_body)
        # 内容通常在 __init__ 之后才被 pack 进来，这里补一次延后同步
        self.after_idle(self._sync_size)

    # ---- 事件入口：一律去抖，避免 Configure 风暴 ----

    def _on_configure(self, _e=None):
        """canvas 尺寸变化 → 合并到下一帧统一重绘。

        原来直接在 Configure 里 `delete("bgshape")` + 重画 + `_sync_size()`，
        而 `_sync_size` 又会 `configure(height=...)` 触发新的 Configure，
        形成「重绘 → 改尺寸 → 再重绘」的高频循环。窗口一被拖动/最大化
        就疯狂重排，这是界面卡顿的主因之一。
        """
        if self._redraw_id is not None:
            return
        try:
            self._redraw_id = self.after_idle(self._flush)
        except tk.TclError:
            pass

    def _on_body(self, _e=None):
        """内容尺寸变化 → 去抖后重算卡片高度。"""
        if self._sync_id is not None:
            return
        try:
            self._sync_id = self.after_idle(self._flush_sync)
        except tk.TclError:
            pass

    def _flush(self):
        self._redraw_id = None
        self._redraw()
        # ⚠️ 必须同时重算尺寸：body 是通过 canvas 的 window item 承载的，
        # 它的宽度由 _sync_size 里的 itemconfigure(width=...) 决定。
        # 这里漏调过 —— 结果是「canvas 变宽了但 body 还停在初始宽度」，
        # 卡片右侧的内容被父容器裁掉、完全不显示（表现为按钮/文字凭空消失）。
        # 为什么只有这里能兜住：body 被钉在旧宽度上，它自己的 <Configure>
        # 不会再触发，所以 _on_body 那条路永远不会重来一次。
        self._sync_size()

    def _flush_sync(self):
        self._sync_id = None
        self._sync_size()

    def _sync_size(self):
        """把 canvas 高度对齐到内容高度（内容变化 → 卡片长高）。"""
        if self._syncing or not alive(self._cv):
            return
        self._syncing = True
        try:
            h = self.body.winfo_reqheight() + self.padding * 2
            w = self._cv.winfo_width()
            size = (w, h)
            # 尺寸没变就完全不动，避免 configure 触发新的 Configure 事件
            if h > 1 and size != self._last_size:
                self._last_size = size
                if self._cv.winfo_height() != h:
                    self._cv.configure(height=h)
                if w > 1:
                    self._cv.itemconfigure(self._win,
                                           width=max(1, w - self.padding * 2))
        except tk.TclError:
            pass
        finally:
            self._syncing = False

    def _redraw(self):
        if not alive(self._cv):
            return
        t = self.theme
        cv = self._cv
        try:
            w, h = cv.winfo_width(), cv.winfo_height()
            if w <= 2 or h <= 2:
                return
            cv.delete("bgshape")
            round_rect(cv, 0.5, 0.5, w - 0.5, h - 0.5, self.radius,
                       fill=t.bg_soft, outline=t.border, width=1, tags="bgshape")
            if self.accent:
                cv.create_rectangle(1, 1, w - 1, 3, fill=t.accent_bar(self.accent),
                                    outline="", tags="bgshape")
            cv.tag_lower("bgshape")
        except tk.TclError:
            pass

    def sync(self):
        """内容变化后手动调用（或依赖 body <Configure> 自动触发）。"""
        if alive(self):
            try:
                self._sync_id = self.after_idle(self._flush_sync)
            except tk.TclError:
                pass

    def refresh(self):
        self._last_size = None
        self._cv.configure(bg=self.theme.bg)
        self.body.configure(bg=self.theme.bg_soft)
        self._redraw()
        self._sync_size()


class Badge(tk.Canvas):
    """胶囊徽标（chip）：语义色底 + 同色系文字。

    注意 1：**绝对不要用 `self._w` / `self._h` 存尺寸**。`_w` 是 Tkinter 内部
    保存控件路径名的属性（如 `.!frame.!canvas`），一旦被像素宽度覆盖，
    后续 pack/place 就会抛
    `TclError: bad argument "77": must be name of window`。
    需要存尺寸请用 `self._cw` / `self._ch`。

    注意 2：绘制必须延后到控件真正注册进 Tk 之后（after_idle），否则
    delete/create 会在尚未注册的 widget 上执行。
    """

    def __init__(self, master, theme, text, tone="slate", icon="", font=None):
        self.theme = theme
        self.tone = tone
        self._font = font or theme.f_tiny
        label = f"{icon} {text}".strip() if icon else text
        self._label = label
        # 先用字体度量估算尺寸（此时 root 已存在，Font() 可用）
        w, h = self._measure(label)
        super().__init__(master, width=w, height=h, bg=theme.bg,
                         highlightthickness=0, bd=0)
        self._cw, self._ch = w, h
        # 延后首次绘制，确保 widget 已注册
        self.after_idle(self._draw)

    def _measure(self, label):
        try:
            f = tkfont.Font(font=self._font)
            w = f.measure(label) + 18
            h = f.metrics("linespace") + 8
        except Exception:  # noqa: BLE001
            w = len(str(label)) * 8 + 18
            h = 22
        return max(28, w), max(18, h)

    def _draw(self):
        if not alive(self):
            return
        fg, bg = self.theme.tone(self.tone)
        try:
            self.delete("all")
            round_rect(self, 0.5, 0.5, self._cw - 0.5, self._ch - 0.5,
                       self._ch / 2, fill=bg, outline="")
            self.create_text(self._cw / 2, self._ch / 2, text=self._label,
                             fill=fg, font=self._font)
        except tk.TclError:
            pass

    def set(self, text, tone=None):
        self._label = text
        if tone:
            self.tone = tone
        self._cw, self._ch = self._measure(text)
        try:
            self.configure(width=self._cw, height=self._ch)
        except tk.TclError:
            return
        self._draw()

    def refresh(self):
        if not alive(self):
            return
        try:
            self.configure(bg=self.theme.bg)
        except tk.TclError:
            return
        self._draw()


def button(master, theme, text, command=None, variant="primary", icon="",
           width=None, small=False):
    """按钮：primary(实心深色) / outline(描边) / ghost(无边框) / danger(危险)。"""
    label = f"{icon} {text}".strip() if icon else text
    palettes = {
        "primary": (theme.accent_fg, theme.accent, theme.accent_hover, ""),
        "outline": (theme.fg_muted, theme.bg_soft, theme.bg_hover, theme.border_soft),
        "ghost": (theme.fg_muted, theme.bg_soft, theme.bg_hover, ""),
        "danger": ("#ffffff", theme.red_fg, theme.red_bg, ""),
    }
    fg, bg, hover, bord = palettes.get(variant, palettes["primary"])
    b = tk.Button(master, text=label, command=command,
                  font=theme.f_btn_sm if small else theme.f_btn,
                  fg=fg, bg=bg, activeforeground=fg, activebackground=hover,
                  relief="flat", bd=0, highlightthickness=1 if bord else 0,
                  highlightbackground=bord or bg, highlightcolor=bord or bg,
                  padx=10 if small else 14, pady=2 if small else 6,
                  cursor="hand2")
    if width:
        b.configure(width=width)
    b.bind("<Enter>", lambda e: b.configure(bg=hover))
    b.bind("<Leave>", lambda e: b.configure(bg=bg))
    return b


class IconButton(tk.Button):
    """纯图标按钮（标题栏最小化/最大化/关闭、刷新等）。

    尺寸固定为 46x40，悬停换底色 —— 与 Windows 原生窗口按钮观感一致。
    """

    def __init__(self, master, theme, text, command=None, hover_bg=None,
                 hover_fg=None):
        self._base_bg = theme.bg_titlebar
        self._hover_bg = hover_bg or theme.bg_hover
        self._base_fg = theme.fg_muted
        self._hover_fg = hover_fg or theme.fg
        super().__init__(master, text=text, command=command,
                         font=(theme.font_family, 10),
                         fg=self._base_fg, bg=self._base_bg,
                         activeforeground=self._hover_fg,
                         activebackground=self._hover_bg,
                         relief="flat", bd=0, highlightthickness=0,
                         width=4, height=1, cursor="hand2",
                         takefocus=0)
        self.bind("<Enter>", lambda e: self.configure(bg=self._hover_bg,
                                                      fg=self._hover_fg))
        self.bind("<Leave>", lambda e: self.configure(bg=self._base_bg,
                                                      fg=self._base_fg))

    def refresh(self, theme):
        self._base_bg = theme.bg_titlebar
        self._base_fg = theme.fg_muted
        self.configure(bg=self._base_bg, fg=self._base_fg)


class Progress(tk.Canvas):
    """细圆角进度条。"""

    def __init__(self, master, theme, height=8):
        super().__init__(master, width=1, height=height, bg=theme.bg_soft,
                         highlightthickness=0, bd=0)
        self.theme = theme
        self._ch = height          # 不用 _h：会覆盖 Tkinter 内部控件路径名
        self.value, self.maximum = 0, 100
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, value, maximum=None):
        self.value = value
        if maximum is not None:
            self.maximum = max(1, maximum)
        self._draw()

    def _draw(self):
        if not alive(self):
            return
        t = self.theme
        try:
            self.delete("all")
            w, h = self.winfo_width(), self._ch
            if w <= 2:
                return
            round_rect(self, 0, 0, w, h, h / 2, fill=t.bg_muted, outline="")
            pct = 0 if self.maximum <= 0 else min(1.0, self.value / self.maximum)
            if pct > 0:
                fw = max(h, w * pct)
                round_rect(self, 0, 0, fw, h, h / 2, fill=t.fg, outline="")
        except tk.TclError:
            pass

    def refresh(self):
        if not alive(self):
            return
        try:
            self.configure(bg=self.theme.bg_soft)
        except tk.TclError:
            return
        self._draw()


class StatCard(tk.Frame):
    """统计卡：标签 + 大数值 + 提示 + 顶部强调条。"""

    def __init__(self, master, theme, label, value="-", hint="", tone="slate",
                 min_w=150, min_h=102):
        super().__init__(master, bg=theme.bg)
        self.theme = theme
        # 卡片外层给一个最小尺寸，避免在 grid/place 里塌成 1px。
        # ⚠️ min_h 只是**下限**：真实高度在 _fit_height() 里按内容算出来。
        # 之前写死 102，而 f_tiny(9pt) 在 125% DPI 下三行内容实际需要 107px，
        # 于是每张统计卡底部的「提示行」都被裁掉约 5px —— 全局 12 张卡都中招。
        # 不再猜固定值，改为「内容需要多少就给多少」，字体/DPI 再变也不会裁。
        self.configure(width=min_w, height=min_h)
        self.pack_propagate(False)
        self.grid_propagate(False)
        self._min_h = min_h
        self._fitted_h = 0
        card = Card(self, theme, accent=tone, padding=13)
        card.pack(fill="both", expand=True)
        self.card = card
        b = card.body
        self._lbl = tk.Label(b, text=label, font=theme.f_card_title,
                             fg=theme.fg_muted, bg=theme.bg_soft, anchor="w")
        self._lbl.pack(fill="x")
        self._val = tk.Label(b, text=value, font=theme.f_stat,
                             fg=theme.value_fg(tone), bg=theme.bg_soft, anchor="w")
        self._val.pack(fill="x", pady=(3, 1))
        self._hint = tk.Label(b, text=hint, font=theme.f_tiny,
                              fg=theme.fg_dim, bg=theme.bg_soft, anchor="w")
        self._hint.pack(fill="x")
        self.tone = tone
        card.sync()
        # 内容此时才齐，延后一帧量一次真实需求高度
        try:
            self.after_idle(self._fit_height)
        except tk.TclError:
            pass

    def _fit_height(self):
        """把外框高度对齐到内容真实需求（不低于 min_h）。

        只在需要长高时 configure，且用 _fitted_h 短路，避免
        「改高度 → 触发 Configure → 再算 → 再改」的抖动。
        """
        try:
            if not self.winfo_exists():
                return
            need = self.card.body.winfo_reqheight() + self.card.padding * 2
        except tk.TclError:
            return
        need = max(self._min_h, need)
        if need == self._fitted_h:
            return
        self._fitted_h = need
        try:
            self.configure(height=need)
            self.card.sync()
        except tk.TclError:
            pass

    def set(self, value=None, hint=None, tone=None):
        if value is not None:
            self._val.configure(text=str(value))
        if hint is not None:
            self._hint.configure(text=hint)
        if tone and tone != self.tone:
            self.tone = tone
            self._val.configure(fg=self.theme.value_fg(tone))
            self.card.accent = tone
            self.card.refresh()
        self.card.sync()
        self._fit_height()

    def refresh(self):
        for w in (self._lbl, self._val, self._hint):
            w.configure(bg=self.theme.bg_soft)
        self.configure(bg=self.theme.bg)
        self.card.theme = self.theme
        self.card.refresh()


class EmptyState(tk.Frame):
    """空状态：虚线框 + 图标 + 标题 + 说明。"""

    def __init__(self, master, theme, icon="", title="", hint=""):
        super().__init__(master, bg=theme.bg)
        self.theme = theme
        box = tk.Frame(self, bg=theme.bg_soft, highlightthickness=1,
                       highlightbackground=theme.border_soft,
                       highlightcolor=theme.border_soft)
        box.pack(fill="both", expand=True, pady=4)
        if icon:
            tk.Label(box, text=icon, font=(theme.font_family, 26),
                     fg=theme.fg_dim, bg=theme.bg_soft).pack(pady=(26, 6))
        tk.Label(box, text=title, font=theme.f_card_title, fg=theme.fg_muted,
                 bg=theme.bg_soft).pack()
        if hint:
            tk.Label(box, text=hint, font=theme.f_small, fg=theme.fg_dim,
                     bg=theme.bg_soft, wraplength=420, justify="center").pack(pady=(4, 26))
        else:
            tk.Label(box, text="", bg=theme.bg_soft).pack(pady=(0, 20))


# --------------------------------------------------------------------------
# 全局滚轮路由
# --------------------------------------------------------------------------
#
# 为什么必须是「全局唯一 + 按指针派发」
# ------------------------------------
# 曾经的做法是把 `<MouseWheel>` 逐个绑到 ScrollFrame 的子树控件上。它坏在
# 一个 Tk 的基本事实上：**事件不沿父子链冒泡**。Tk 只按 bindtags 派发
# （控件 → 控件类 → 顶层 → "all"），父控件不在子控件的 bindtags 里。
# 所以把滚轮绑在内容 frame 上，事件落在它的子 Label / Card 上时**根本不会
# 触发**。而页面内容是运行期才塞进去的，绑不胜绑 —— 用户感受就是
# 「滚轮没反应，只能按住右边滚动条拖」。
#
# 三个候选方案里只有第三个是对的：
#   ✗ 逐个绑子控件：动态新增的控件漏绑；页面还得记得补绑（本项目就是这么坏的）
#   ✗ 每个容器各自 bind_all：多个容器互相覆盖/争抢，unbind_all 更会把别人的也清掉
#   ✓ **全局只装一个处理函数，按指针位置找最近的可滚动祖先，只滚它一个**
#     既不会漏（不依赖谁绑过），也不会有两个容器抢同一个事件（同一时刻
#     指针只可能在一个容器里）。
#
# 注意 `bind_all` 本身不是问题，「每个容器都 bind_all + 还 unbind_all」才是。
# 这里全进程只装一次，且从不 unbind。

_WHEEL = {"installed": False, "acc": 0.0}

# 活跃的 ScrollFrame（弱引用，页面销毁后自动摘掉，不会越积越多）
_FRAMES = weakref.WeakSet()


def _can_scroll_canvas(canvas):
    """canvas 的内容是否超出可视区（超出才需要滚）。"""
    try:
        first, last = canvas.yview()
        return (last - first) < 0.999
    except tk.TclError:
        return False


def _can_scroll_native(widget):
    """原生可滚动控件（Text/Listbox/Treeview）内容是否超出可视区。"""
    try:
        first, last = widget.yview()
        return (last - first) < 0.999
    except Exception:  # noqa: BLE001
        # 判断不了就当它能滚 —— 宁可让它自己处理，也别抢它的滚轮
        return True


def wheel_container(widget):
    """从 `widget` 往上找最近的、**且真的能滚**的容器。

    返回 ScrollFrame 或原生可滚动控件；找不到返回 None。
    只用于「指针下那个控件是不是原生可滚控件」这一判断，以及兜底。
    """
    w = widget
    while w is not None:
        if isinstance(w, ScrollFrame):
            if _can_scroll_canvas(w.canvas):
                return w
        elif isinstance(w, (tk.Text, tk.Listbox, ttk.Treeview)):
            if _can_scroll_native(w):
                # 能滚的原生控件优先自己处理，别再滚外层页面（否则会双滚）
                return w
            # 内容装得下 → 不拦，继续往上找页面容器
        w = getattr(w, "master", None)
    return None


def frames_at(x_root, y_root, top=None):
    """列出指针下的滚动容器，**从最内层到最外层**。

    为什么用几何位置而不是 `winfo_containing`
    ------------------------------------------
    `winfo_containing` 底层是 Win32 的 `WindowFromPoint`：**只要那个点被
    别的窗口挡住就返回空**。窗口被遮挡（或自动化测试里终端压在应用上）时，
    指针明明在应用里，滚轮却会静默失效 —— 这个失效模式很难查。
    改成直接比几何：谁的可视矩形包含指针，谁就是候选；面积小的在内层。

    `top` 是**当前活动窗口**（收到事件的那个 toplevel），必须按它过滤
    --------------------------------------------------------------
    只比几何有个反面：**矩形判断完全不知道窗口遮挡**。模态框是独立的
    `Toplevel`、居中盖在页面上（见 `Modal.show()`），它的矩形落在页面的
    canvas 矩形之内 —— 不按窗口过滤的话，指针明明在弹窗上，这里却会命中
    **弹窗后面那一页**的 canvas，于是滚轮把背后的页面滚掉了。
    （实测：弹窗 520x260+508+200，弹窗中心命中"本页"，yview 0.5000 → 0.5334。）

    所以只考虑**与活动窗口同一个 toplevel** 的容器：弹窗打开时，事件属于
    弹窗，页面容器一律不参与；弹窗自己若带滚动容器，照样能被滚。
    """
    hits = []
    for sf in list(_FRAMES):
        try:
            if not sf.winfo_exists():
                continue
            if top is not None and sf.winfo_toplevel() is not top:
                continue                   # 属于别的窗口（被弹窗盖住的那一页）
            cv = sf.canvas
            # ★ 必须过滤「已经不在屏幕上的」滚动容器（2026-09-22 修）
            #   本函数遍历的是**全局注册表 `_FRAMES`**（所有创建过的 ScrollFrame），
            #   原先只判 `winfo_exists()`。页面缓存上线后，15 个页面的 ScrollFrame
            #   **全部存活**、canvas 矩形又完全重叠（都占 page_host 那块区域），
            #   于是命中列表里混进了**隐藏页面**的容器；下面按面积排序时它们
            #   面积相同 → 顺序看运气 → 滚轮可能滚到看不见的那一页去，
            #   用户看到的就是「滚了没反应」。
            #   实测：`test_wheel_scroll` 从 54 OK / 0 FAIL 变成 40 OK / 14 FAIL。
            if not cv.winfo_ismapped():
                continue
            cx, cy = cv.winfo_rootx(), cv.winfo_rooty()
            cw, ch = cv.winfo_width(), cv.winfo_height()
        except tk.TclError:
            continue
        if cw <= 1 or ch <= 1:
            continue
        if cx <= x_root < cx + cw and cy <= y_root < cy + ch:
            hits.append((cw * ch, sf))
    hits.sort(key=lambda t: t[0])          # 面积小的 = 更内层，优先
    return [sf for _area, sf in hits]


def deepest_at(root, x_root, y_root):
    """按几何位置找指针下**最深**的控件（不依赖 `winfo_containing`）。

    为什么不用 `winfo_containing`
    ----------------------------
    它底层是 Win32 的 `WindowFromPoint`：**那个点被任何别的窗口挡住就返回
    None**。终端压在应用上、或者有个置顶的小工具飘过，指针明明在应用里，
    这一步就静默失效 —— 于是"指针下的原生滚动框"判断被整个跳过。这个失效
    模式极难复现（取决于当时桌面窗口的层叠），所以必须绕开。

    纯几何做法：从顶层窗口开始，每一层只挑**矩形包含该点**的那个子控件往
    下走（后面的兄弟控件叠在上面，所以倒序遍历），直到没有子控件包含该点
    为止。只走一条分支，每层 O(子控件数)，与遮挡无关、也不需要窗口置顶。
    """
    cur = root
    for _ in range(64):                     # 防御：万一几何有环
        nxt = None
        try:
            children = cur.winfo_children()
        except tk.TclError:
            break
        for c in reversed(children):
            try:
                if not c.winfo_ismapped():
                    continue
                cw, ch = c.winfo_width(), c.winfo_height()
                if cw <= 1 or ch <= 1:
                    continue
                cx, cy = c.winfo_rootx(), c.winfo_rooty()
            except tk.TclError:
                continue
            if cx <= x_root < cx + cw and cy <= y_root < cy + ch:
                nxt = c
                break
        if nxt is None:
            break
        cur = nxt
    return cur


def _route_wheel(event):
    """全局滚轮处理：按**指针位置**决定滚哪个容器。

    Windows 上滚轮按平台惯例送给指针下的窗口（与键盘焦点无关），所以
    指针位置才是唯一可靠的判据 —— 不能靠"谁有焦点"。
    """
    try:
        x_root = getattr(event, "x_root", None)
        y_root = getattr(event, "y_root", None)
        if x_root is None or y_root is None:
            return None

        # 1) 指针下是不是一个**自己能滚**的原生控件（日志框、输出框…）？
        #    是就让它自己处理：它的类绑定已经跑过了（"all" 是最后一个
        #    bindtag），这里什么都不做，也不能返回 "break" 去干扰。
        #    ⚠️ 这里必须用几何定位，不能用 winfo_containing（见 deepest_at）。
        try:
            top = event.widget.winfo_toplevel()
        except Exception:  # noqa: BLE001
            top = None
        if top is not None:
            native = wheel_container(deepest_at(top, x_root, y_root))
            if native is not None and not isinstance(native, ScrollFrame):
                return None

        # 2) 找指针下最内层、且真的能滚的 ScrollFrame。
        #    内层装得下就自动往外层找（嵌套列表 + 页面滚动的情况）。
        #    ⚠️ 必须传 `top`：只按矩形找的话，模态框盖在页面上时会把**后面
        #    那一页**滚掉（几何判断不知道窗口遮挡）。
        target = None
        for sf in frames_at(x_root, y_root, top):
            if _can_scroll_canvas(sf.canvas):
                target = sf
                break
        if target is None:
            return None

        delta = getattr(event, "delta", 0)
        if delta:
            steps = -delta / 120.0
        elif getattr(event, "num", None) == 4:      # X11 上滚
            steps = -1.0
        elif getattr(event, "num", None) == 5:      # X11 下滚
            steps = 1.0
        else:
            return None

        _scroll_units(target.canvas, steps)
        return "break"
    except Exception:  # noqa: BLE001
        return None


def _scroll_units(canvas, steps):
    """按「单位」滚动，带小数累积。

    有些鼠标/触控板的 delta 不是 120 的整数倍（常见 40、100），
    直接 `int(delta/120)` 会全被截断成 0 —— 表现为"滚了没反应"。
    这里把不足一格的零头攒起来，攒够一格再滚。
    """
    acc = _WHEEL["acc"] + steps
    whole = int(acc)
    _WHEEL["acc"] = acc - whole
    if not whole:
        return
    try:
        canvas.yview_scroll(whole, "units")
    except tk.TclError:
        pass


def install_wheel_router(widget):
    """装上全局滚轮路由（整个进程只装一次，重复调用无副作用）。

    在 `ScrollFrame.__init__` 里自动调用，所以只要用过滚动容器就已经装好；
    不需要页面或 App 额外做什么。
    """
    if _WHEEL["installed"]:
        return
    try:
        widget.bind_all("<MouseWheel>", _route_wheel)
        widget.bind_all("<Button-4>", _route_wheel)     # Linux 上滚
        widget.bind_all("<Button-5>", _route_wheel)     # Linux 下滚
    except Exception:  # noqa: BLE001
        return
    _WHEEL["installed"] = True


class ScrollFrame(tk.Frame):
    """可滚动容器：返回 .inner 作为内容父级，支持鼠标滚轮。

    性能说明（第一版很卡的原因）
    ---------------------------
    原实现在 `<Configure>` 里对 canvas 和 inner 各绑一次 `<Enter>/<Leave>`，
    并且用 `bind_all("<MouseWheel>")` 抢全局滚轮：
      * 每次 Configure（拖窗口、最大化都会高频触发）都重复 `bind`，
        同一个控件上堆积了越来越多的重复绑定；
      * `bind_all` 是全局的，多个滚动容器（每页一个，加上设置页里还有）
        会互相覆盖/争抢，先 bind 的容器会一直收到本不属于它的事件；
      * `unbind_all` 更危险——它会把**所有** MouseWheel 绑定清掉，
        包括别处挂的。
    改法：
      * 绑定只在 `__init__` 里做一次；
      * 滚轮走**全局唯一路由**（`install_wheel_router`）：按指针位置找最近的
        可滚动祖先，只滚它一个。绑在子控件上的老做法是坏的 —— Tk 事件不沿
        父子链冒泡，落在子控件上的滚轮事件收不到（详见上面的长注释）；
      * 翻页/改尺寸用节流的 resync，而不是每帧重算。
    """

    def __init__(self, master, theme, bg=None):
        # `bg` 默认沿用 `theme.bg`（页面底色）。放在 `bg_soft` 的容器里
        # （卡片、模态框内容区）时**必须显式传 `bg=theme.bg_soft`** ——
        # 否则 canvas/inner 会比周围深一档，内容装不满时露出一整块异色区域
        # （浅色主题 #f1f5f9 vs #ffffff，非常显眼）。
        bg = theme.bg if bg is None else bg
        self._bg = bg
        super().__init__(master, bg=bg)
        self.theme = theme
        # width=1 覆盖 tk.Canvas 默认的 378，否则会把页面撑出可视区
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0,
                                width=1)
        self.vbar = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                 relief="flat", bd=0, width=10,
                                 troughcolor=bg, bg=theme.bg_muted,
                                 activebackground=theme.fg_dim)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.vbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window(0, 0, anchor="nw", window=self.inner)

        # 滚动区域只在内容尺寸真变化时更新（原来是每次 Configure 都算）
        self._last_bbox = None
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)

        # 滚轮：交给全局唯一路由（见 install_wheel_router），这里不用再绑
        self._wheel_on = False
        self._resync_id = None
        _FRAMES.add(self)                 # 供全局路由按几何位置找到自己
        install_wheel_router(self)

    # ---------- 尺寸同步 ----------

    def _on_inner(self, _e=None):
        """内容尺寸变化 → 更新 scrollregion（去抖，避免高频重算）。"""
        bbox = self.canvas.bbox("all")
        if bbox and bbox != self._last_bbox:
            self._last_bbox = bbox
            self.canvas.configure(scrollregion=bbox)

    def _on_canvas(self, event):
        """把内容宽度锁定为画布宽度，避免子控件 requested width 外溢。"""
        try:
            self.canvas.itemconfigure(self._win, width=max(1, event.width))
        except tk.TclError:
            pass
        # 宽度变了 → 高度可能因换行而变，重新算一次 scrollregion
        self._on_inner()

    def resync(self):
        """窗口尺寸突变后强制重算（最大化 / 还原 / 分栏拖动时调用）。

        先把当前挂着的重算取消掉，再在 idle 之后统一做一次，
        避免连续多次调用导致同一帧里重复计算。
        """
        try:
            if self._resync_id is not None:
                self.after_cancel(self._resync_id)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._resync_id = self.after_idle(self._resync_now)
        except Exception:  # noqa: BLE001
            self._resync_now()

    def _resync_now(self):
        self._resync_id = None
        if not alive(self.canvas):
            return
        try:
            self.canvas.update_idletasks()
            w = self.canvas.winfo_width()
            if w > 1:
                self.canvas.itemconfigure(self._win, width=max(1, w))
            self._last_bbox = None
            self._on_inner()
            # 让卡片重新量高（它们在 Configure 里回写自身高度）
            for child in self.inner.winfo_children():
                if hasattr(child, "sync"):
                    try:
                        child.sync()
                    except Exception:  # noqa: BLE001
                        pass
        except tk.TclError:
            pass

    # ---------- 滚轮 ----------

    def bind_wheel_to(self, widget, depth=0):
        """（保留的兼容入口，现在**不需要调用**了。）

        历史：滚轮曾经靠「把 <MouseWheel> 逐个绑到子控件上」实现，于是页面
        每动态加一批控件就得记得补绑。没有任何一个页面调用过它，结果就是
        「滚轮没反应、只能拖滚动条」。现在改成全局路由（见
        `install_wheel_router`），按指针位置找容器，新增控件自动生效。
        保留这个方法是为了不破坏已有调用点，它现在什么都不用做。
        """
        return widget

    def _is_mine(self, widget):
        """判断控件是否是本滚动容器的后代。"""
        try:
            w = widget
            while w is not None:
                if w is self:
                    return True
                parent = getattr(w, "master", None)
                if parent is None:
                    return False
                w = parent
            return False
        except Exception:  # noqa: BLE001
            return True

    def scroll_top(self):
        self.canvas.yview_moveto(0)

    def refresh(self):
        # ⚠️ 用 `self._bg` 而不是 `theme.bg` —— 否则切主题会把 `bg=bg_soft`
        # 的容器打回页面底色，又露出那块异色区域。
        self.configure(bg=self._bg)
        self.canvas.configure(bg=self._bg)
        self.inner.configure(bg=self._bg)
        self.vbar.configure(troughcolor=self._bg, bg=self.theme.bg_muted,
                            activebackground=self.theme.fg_dim)


class Modal(tk.Toplevel):
    """居中模态框：标题 + 内容 + 底部按钮，Esc 关闭。"""

    def __init__(self, master, theme, title, width=520, height=260):
        super().__init__(master, bg=theme.border)
        self.theme = theme
        self.withdraw()
        self.overrideredirect(True)
        self.transient(master)
        self.title(title)
        outer = tk.Frame(self, bg=theme.bg_soft, highlightthickness=1,
                         highlightbackground=theme.border)
        outer.pack(fill="both", expand=True, padx=1, pady=1)
        head = tk.Frame(outer, bg=theme.bg_soft)
        head.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(head, text=title, font=theme.f_title, fg=theme.fg,
                 bg=theme.bg_soft).pack(side="left")
        IconButton(head, theme, "✕", command=self.close,
                   hover_bg=theme.red_bg, hover_fg=theme.red_fg).pack(side="right")
        tk.Frame(outer, bg=theme.border, height=1).pack(fill="x", padx=16)
        self.content = tk.Frame(outer, bg=theme.bg_soft)
        self.content.pack(fill="both", expand=True, padx=16, pady=12)
        self.footer = tk.Frame(outer, bg=theme.bg_soft)
        self.footer.pack(fill="x", padx=16, pady=(0, 14))
        self._mw, self._mh = width, height
        self.bind("<Escape>", lambda e: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)

    def show(self):
        self.update_idletasks()
        m = self.master.winfo_toplevel()
        x = m.winfo_rootx() + (m.winfo_width() - self._mw) // 2
        y = m.winfo_rooty() + (m.winfo_height() - self._mh) // 3
        self.geometry(f"{self._mw}x{self._mh}+{max(0, x)}+{max(0, y)}")
        self.deiconify()
        self.lift()
        self.grab_set()
        self.focus_force()

    def fit_height(self, min_h=None, max_h=620):
        """按内容自然高度**只增不减**地调整弹窗高度（在 `show()` 前调用）。

        为什么需要它
        ------------
        `Modal` 是**定尺寸**的。定尺寸弹窗里放「变长内容」（用户可控的昵称、
        路径、被 `wraplength` 折行的长句）时，超出的部分会**静默消失** ——
        pack 空间不够时会把后面的孩子**压扁**，所以既看不到、也滚不动，
        而且没有任何报错或视觉痕迹（就是"下面没有了"）。
        这和「账号池明细」是同一类缺陷，区别只是那边内容无上界（N 个账号，
        必须用滚动容器），这边是固定的几行、只是个别值会折行，所以
        **长高到刚好放下**才是对的观感：装得下就完全不动。

        注意量的是 `winfo_reqheight()`（孩子真正需要的高度），不是实际分到的
        高度 —— 后者被压扁过，永远"装得下"。
        """
        try:
            self.update_idletasks()
            need = self.winfo_reqheight()
        except tk.TclError:
            return self._mh
        floor = self._mh if min_h is None else max(int(min_h), 0)
        self._mh = int(max(floor, min(need, max_h)))
        return self._mh

    def close(self):
        try:
            self.grab_release()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()


class Switch(tk.Canvas):
    """开关控件：圆角轨道 + 滑块，对齐参考项目的 checkbox 语义。

    尺寸存 `_cw`/`_ch` —— `_w` 是 Tkinter 内部控件路径名，不可占用。
    """

    def __init__(self, master, theme, command=None, value=False):
        self._cw, self._ch = 36, 20
        super().__init__(master, width=self._cw, height=self._ch, bg=theme.bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.theme = theme
        self.value = value
        self.command = command
        self.after_idle(self._draw)
        self.bind("<Button-1>", self._toggle)

    def _draw(self):
        if not alive(self):
            return
        t = self.theme
        try:
            self.delete("all")
            on = self.value
            track = t.accent if on else t.bg_muted
            round_rect(self, 0, 0, self._cw, self._ch, self._ch / 2,
                       fill=track, outline="")
            cx = self._cw - self._ch / 2 - 2 if on else self._ch / 2 + 2
            knob = t.accent_fg if on else t.bg_soft
            self.create_oval(cx - self._ch / 2 + 3, 3, cx + self._ch / 2 - 3,
                             self._ch - 3, fill=knob, outline="")
        except tk.TclError:
            pass

    def _toggle(self, _e=None):
        self.value = not self.value
        self._draw()
        if self.command:
            self.command(self.value)

    def set(self, value):
        self.value = bool(value)
        self._draw()

    def refresh(self):
        if not alive(self):
            return
        try:
            self.configure(bg=self.theme.bg)
        except tk.TclError:
            return
        self._draw()


class Chart(tk.Canvas):
    """轻量柱状图：用于积分 Top 榜（参考项目用 recharts，这里用 Canvas 手绘）。"""

    def __init__(self, master, theme, height=240):
        # width=1 覆盖 tk.Canvas 默认 378，避免撑宽父容器
        super().__init__(master, width=1, height=height, bg=theme.bg_soft,
                         highlightthickness=0, bd=0)
        self.theme = theme
        self.data = []          # [(name, value)]
        self.empty_hint = "暂无数据"
        self.bind("<Configure>", lambda e: self._draw())

    def set_data(self, data):
        """data: ``[(name, value)]`` 或 ``[(name, value, pct_text)]``。

        第三项是可选的**占比文本**（如 ``"23%"``），有就画在数值标签上方。
        保持对 2 元组的兼容 —— 其它调用点不用改。
        """
        self.data = [tuple(d) for d in (data or [])]
        self._draw()

    def _draw(self):
        if not alive(self):
            return
        t = self.theme
        try:
            self.delete("all")
            w = self.winfo_width()
            h = self.winfo_height()
        except tk.TclError:
            return
        if w < 40 or h < 40:
            return
        if not self.data:
            self.create_text(w / 2, h / 2, text=self.empty_hint, fill=t.fg_dim,
                             font=t.f_small)
            return
        # 带占比时顶部要多留一行（占比画在数值上方）
        _has_pct = any(len(d) > 2 and d[2] for d in self.data)
        pad_l, pad_r, pad_t, pad_b = 8, 8, (36 if _has_pct else 26), 44
        cw = w - pad_l - pad_r
        ch = h - pad_t - pad_b
        n = len(self.data)
        slot = cw / max(1, n)
        bar_w = min(46, slot * 0.55)
        vmax = max([d[1] for d in self.data] + [1])
        # 网格线
        for i in range(4):
            y = pad_t + ch * i / 3
            self.create_line(pad_l, y, w - pad_r, y, fill=t.grid, dash=(3, 3))
        for i, item in enumerate(self.data):
            name, val = item[0], item[1]
            pct = item[2] if len(item) > 2 else None
            cx = pad_l + slot * (i + 0.5)
            bh = max(2, ch * (val / vmax))
            x0, x1 = cx - bar_w / 2, cx + bar_w / 2
            y0, y1 = pad_t + ch - bh, pad_t + ch
            # Top3 用强调色，其余渐淡
            if t.dark:
                colors = ["#fafafa", "#e4e4e7", "#d4d4d8"]
                fill = colors[i] if i < 3 else "#a1a1aa"
            else:
                colors = ["#27272a", "#3f3f46", "#52525b"]
                fill = colors[i] if i < 3 else "#94a3b8"
            round_rect(self, x0, y0, x1, y1, 6, fill=fill, outline="")
            # 数值标签
            txt = f"{val/1000:.1f}k" if val >= 1000 else f"{val:.0f}"
            self.create_text(cx, y0 - 9, text=txt, fill=t.fg_muted,
                             font=(t.font_family, 8))
            if pct:
                self.create_text(cx, y0 - 21, text=pct, fill=t.fg_dim,
                                 font=(t.font_family, 8))
            # 名字（过长截断）
            label = name if len(name) <= 10 else name[:9] + "…"
            self.create_text(cx, pad_t + ch + 12, text=label, fill=t.fg_dim,
                             font=(t.font_family, 8), width=slot - 4,
                             anchor="n")

    def refresh(self):
        if not alive(self):
            return
        try:
            self.configure(bg=self.theme.bg_soft)
        except tk.TclError:
            return
        self._draw()


def section_title(master, theme, text, sub=""):
    """区块标题 + 可选副标题。"""
    box = tk.Frame(master, bg=theme.bg)
    tk.Label(box, text=text, font=theme.f_card_title, fg=theme.fg,
             bg=theme.bg, anchor="w").pack(side="left")
    if sub:
        tk.Label(box, text=sub, font=theme.f_tiny, fg=theme.fg_dim,
                 bg=theme.bg, anchor="w").pack(side="left", padx=(8, 0), pady=(3, 0))
    return box


def hline(master, theme):
    return tk.Frame(master, bg=theme.border, height=1)
