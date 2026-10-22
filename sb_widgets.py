# -*- coding: utf-8 -*-
"""自绘控件: 圆角按钮、分段选择、开关、滑杆、弹出菜单, 以及 Treeview 深色样式。

全部基于 tkinter.Canvas 手绘, 不依赖 ttk 主题差异, 在 Win10/Win11 上表现一致。
"""
from __future__ import annotations

import ctypes
import time
import tkinter as tk
from tkinter import ttk

from sb_theme import UI_FAMILY, darken, draw_round_rect, focus_window, force_topmost, mix


# ---------------- 按钮 ----------------
class CanvasButton(tk.Canvas):
    def __init__(self, master, text, command, pal, fonts, kind="ghost",
                 width=None, height=34, scale=1.0, bg=None, icon=""):
        self.pal = pal
        self.font = fonts.ui(-2)
        self.kind = kind
        self.command = command
        self.scale = scale
        self._state = "normal"
        pad = 16 * scale
        label = (icon + " " + text).strip()
        self.text = label
        w = width * scale if width else self.font.measure(label) + pad * 2
        h = height * scale
        super().__init__(master, width=int(round(w)), height=int(round(h)),
                         bg=bg or pal["panel"], highlightthickness=0, bd=0,
                         cursor="hand2", takefocus=1)
        self.bind("<Enter>", lambda e: self._set_state("hover"))
        self.bind("<Leave>", lambda e: self._set_state("normal"))
        self.bind("<Button-1>", lambda e: self._set_state("press"))
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<FocusIn>", lambda e: self._draw())
        self.bind("<FocusOut>", lambda e: self._draw())
        self.bind("<Return>", lambda e: self._fire())
        self.bind("<space>", lambda e: self._fire())
        self._draw()

    def _set_state(self, state):
        self._state = state
        self._draw()

    def _fire(self):
        if callable(self.command):
            self.command()

    def _on_release(self, event):
        inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        self._state = "hover" if inside else "normal"
        self._draw()
        if inside:
            self._fire()

    def _colors(self):
        pal, state, kind = self.pal, self._state, self.kind
        if kind == "primary":
            base = pal["accent"]
            fill = {"normal": base, "hover": pal["accent_hover"],
                    "press": darken(base, 0.12)}[state]
            return fill, "", pal["accent_text"]
        if kind == "danger":
            fill = pal["pos_soft"] if state == "normal" else mix(pal["pos_soft"], pal["pos"], 0.16)
            return fill, "", pal["pos"]
        if kind == "quiet":
            fill = pal["panel"] if state == "normal" else pal["hover"]
            return fill, "", pal["muted"] if state == "normal" else pal["text"]
        fill = {"normal": pal["panel_alt"], "hover": pal["hover"], "press": pal["press"]}[state]
        return fill, pal["border"], pal["text"]

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1:
            w, h = int(self["width"]), int(self["height"])
        fill, outline, fg = self._colors()
        r = min(9 * self.scale, h / 2)
        draw_round_rect(self, 0.5, 0.5, w - 0.5, h - 0.5, r, fill, outline, 1)
        if self.focus_get() is self:
            draw_round_rect(self, 2.5, 2.5, w - 2.5, h - 2.5, r - 2, "", self.pal["accent"], 1)
        self.create_text(w / 2, h / 2 - 0.5, text=self.text, fill=fg, font=self.font)


# ---------------- 分段选择 ----------------
class Segmented(tk.Canvas):
    def __init__(self, master, options, value, command, pal, fonts, scale=1.0,
                 height=32, item_pad=12, min_item=42, bg=None):
        """options: [(value, label), ...]"""
        self.pal = pal
        self.font = fonts.ui(-2)
        self.options = list(options)
        self.value = value
        self.command = command
        self.scale = scale
        self.item_pad = item_pad * scale
        self.min_item = min_item * scale
        self.item_h = height * scale
        self.gap = 3 * scale
        self.widths = []
        for _v, label in self.options:
            self.widths.append(max(self.min_item, self.font.measure(label) + self.item_pad * 2))
        total = sum(self.widths) + self.gap * 2
        super().__init__(master, width=int(round(total)), height=int(round(self.item_h + 2)),
                         bg=bg or pal["panel"], highlightthickness=0, bd=0, cursor="hand2")
        self.bind("<Button-1>", self._on_click)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda e: self._draw(None))
        self._hover = None
        self._draw()

    def _boxes(self):
        x = 1 * self.scale
        y = 1 * self.scale
        out = []
        for w in self.widths:
            out.append((x, y, x + w, y + self.item_h))
            x += w + self.gap
        return out

    def _index_at(self, x):
        for i, (x1, _y1, x2, _y2) in enumerate(self._boxes()):
            if x1 <= x <= x2:
                return i
        return None

    def _on_motion(self, event):
        idx = self._index_at(event.x)
        if idx != self._hover:
            self._hover = idx
            self._draw()

    def _on_click(self, event):
        idx = self._index_at(event.x)
        if idx is None:
            return
        new_value = self.options[idx][0]
        if new_value == self.value:
            return
        self.value = new_value
        self._draw()
        if callable(self.command):
            self.command(new_value)

    def set(self, value, notify=False):
        self.value = value
        self._draw()
        if notify and callable(self.command):
            self.command(value)

    def _draw(self, _hover=None):
        self.delete("all")
        boxes = self._boxes()
        if not boxes:
            return
        x1, y1 = 0.5, 0.5
        x2 = boxes[-1][2] + 1 * self.scale
        y2 = boxes[0][3] + 1.5 * self.scale
        r = min(9 * self.scale, (y2 - y1) / 2)
        draw_round_rect(self, x1, y1, x2, y2, r, self.pal["panel_alt"], self.pal["border"], 1)
        for i, (bx1, by1, bx2, by2) in enumerate(boxes):
            selected = self.options[i][0] == self.value
            if selected:
                draw_round_rect(self, bx1, by1, bx2, by2, r - 2 * self.scale,
                                self.pal["accent"], "", 1)
                fg = self.pal["accent_text"]
            else:
                if self._hover == i:
                    draw_round_rect(self, bx1, by1, bx2, by2, r - 2 * self.scale,
                                    self.pal["hover"], "", 1)
                fg = self.pal["text_dim"] if self._hover == i else self.pal["muted"]
            self.create_text((bx1 + bx2) / 2, (by1 + by2) / 2 - 0.5,
                             text=self.options[i][1], fill=fg, font=self.font)


# ---------------- 开关 ----------------
class Switch(tk.Canvas):
    def __init__(self, master, value, command, pal, scale=1.0, bg=None, width=44, height=24):
        self.pal = pal
        self.value = bool(value)
        self.command = command
        self.scale = scale
        self.w = int(width * scale)
        self.h = int(height * scale)
        super().__init__(master, width=self.w, height=self.h,
                         bg=bg or pal["panel_alt"], highlightthickness=0, bd=0, cursor="hand2")
        self._pos = 1.0 if self.value else 0.0
        self.bind("<Button-1>", self._toggle)
        self.bind("<Enter>", lambda e: self._draw())
        self.bind("<Leave>", lambda e: self._draw())
        self._hover = False
        self._animate_to(self._pos)

    def _toggle(self, _event=None):
        self.value = not self.value
        self._animate_to(1.0 if self.value else 0.0)
        if callable(self.command):
            self.command(self.value)

    def _animate_to(self, target):
        cancel = getattr(self, "_after", None)
        if cancel:
            try:
                self.after_cancel(cancel)
            except tk.TclError:
                pass
        step = 0.18 if target > self._pos else -0.18

        def tick():
            self._pos += step
            if (step > 0 and self._pos >= target) or (step < 0 and self._pos <= target):
                self._pos = target
                self._after = None
                self._draw()
                return
            self._draw()
            self._after = self.after(14, tick)

        tick()

    def _draw(self):
        self.delete("all")
        w, h = self.w, self.h
        r = h / 2
        if self.value:
            track = self.pal["accent"] if self._hover else self.pal["accent"]
        else:
            track = self.pal["track"]
        draw_round_rect(self, 0.5, 0.5, w - 0.5, h - 0.5, r - 0.5, track, "", 1)
        pad = 2.5 * self.scale
        kd = h - 2 * pad
        kx = pad + (w - 2 * pad - kd) * self._pos
        self.create_oval(kx, pad, kx + kd, pad + kd, fill="#FFFFFF", outline="")

    def set(self, value):
        self.value = bool(value)
        self._pos = 1.0 if self.value else 0.0
        self._draw()


# ---------------- 滑杆 ----------------
class Slider(tk.Canvas):
    def __init__(self, master, value, minimum, maximum, command, pal, fonts, scale=1.0,
                 width=230, height=24, suffix="%", bg=None):
        self.pal = pal
        self.min = minimum
        self.max = maximum
        self.value = value
        self.command = command
        self.font = fonts.num(-2, False)
        self.scale = scale
        self.suffix = suffix
        self.w = int(width * scale)
        self.h = int(height * scale)
        super().__init__(master, width=self.w, height=self.h,
                         bg=bg or pal["panel_alt"], highlightthickness=0, bd=0, cursor="hand2")
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", lambda e: self._commit())
        self._label_w = self.font.measure("100%") + 10 * scale
        self._draw()

    def _track_box(self):
        pad = 8 * self.scale
        return pad, self.w - self._label_w

    def _ratio(self):
        if self.max == self.min:
            return 0.0
        return (self.value - self.min) / (self.max - self.min)

    def _on_press(self, event):
        self._set_from_x(event.x)

    def _on_drag(self, event):
        self._set_from_x(event.x)

    def _set_from_x(self, x):
        x1, x2 = self._track_box()
        ratio = 0.0 if x2 <= x1 else max(0.0, min(1.0, (x - x1) / (x2 - x1)))
        value = int(round(self.min + ratio * (self.max - self.min)))
        if value != self.value:
            self.value = value
            self._draw()
            if callable(self.command):
                self.command(value)

    def _commit(self):
        if callable(self.command):
            self.command(self.value)

    def set(self, value):
        self.value = value
        self._draw()

    def _draw(self):
        self.delete("all")
        x1, x2 = self._track_box()
        cy = self.h / 2
        draw_round_rect(self, x1, cy - 3.5, x2, cy + 3.5, 3.5, self.pal["track"], "", 1)
        px = x1 + (x2 - x1) * self._ratio()
        if px > x1 + 1:
            draw_round_rect(self, x1, cy - 3.5, px, cy + 3.5, 3.5, self.pal["accent"], "", 1)
        self.create_oval(px - 7.5 * self.scale, cy - 7.5 * self.scale,
                         px + 7.5 * self.scale, cy + 7.5 * self.scale,
                         fill="#FFFFFF", outline=self.pal["accent"], width=int(2 * self.scale))
        self.create_text(self.w - 2 * self.scale, cy, text=f"{self.value}{self.suffix}",
                         anchor="e", fill=self.pal["text_dim"], font=self.font)


# ---------------- 弹出菜单 ----------------
class MenuPopup(tk.Toplevel):
    """自绘深色菜单: 支持分隔线/勾选/子菜单, 不依赖系统主题。

    items 每项为 dict:
        {"type": "cmd", "label": "...", "shortcut": "...", "command": fn}
        {"type": "sep"}
        {"type": "check", "label": "...", "checked": bool, "command": fn}
        {"type": "sub", "label": "...", "items": [...]}
    """

    _open_menus: list["MenuPopup"] = []

    def __init__(self, master, items, pal, fonts, scale=1.0, parent_menu=None,
                 owner_hwnd=None, on_close=None):
        super().__init__(master)
        self.pal = pal
        self.fonts = fonts
        self.scale = scale
        self.items = items
        self.parent_menu = parent_menu
        self.submenus: list[MenuPopup] = []
        self.on_close = on_close
        self.owner_hwnd = owner_hwnd
        self._hover = None
        self._sub_after = None
        self._closed = False
        self._press_was_down = self._mouse_down()
        self._outside_since = None

        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=pal["menu_bg"])
        try:
            self.attributes("-transparentcolor", "#010203")
        except tk.TclError:
            pass

        self.font_item = fonts.ui(-2)
        self.font_hint = fonts.num(-3, False)
        self.pad = int(6 * scale)
        self.item_h = int(31 * scale)
        self.sep_h = int(9 * scale)
        self.width = self._measure()
        self.height = self._total_height()
        self.canvas = tk.Canvas(self, width=self.width, height=self.height, bg="#010203",
                                highlightthickness=0, bd=0)
        self.canvas.pack()
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Button-3>", lambda e: self.close_root())
        self.bind("<Escape>", lambda e: self.close_root())
        self._draw()
        MenuPopup._open_menus.append(self)

    # ---- 尺寸 ----
    def _measure(self):
        widest = 190 * self.scale
        for it in self.items:
            if it["type"] == "sep":
                continue
            text_w = self.font_item.measure(it.get("label", ""))
            shortcut = it.get("shortcut", "")
            hint_w = self.font_hint.measure(shortcut) if shortcut else 0
            reserve = (46 if it["type"] == "sub" else 30) * self.scale
            widest = max(widest, text_w + hint_w + reserve + 20 * self.scale)
        return int(min(widest, 330 * self.scale))

    def _total_height(self):
        h = self.pad * 2
        for it in self.items:
            h += self.sep_h if it["type"] == "sep" else self.item_h
        return int(h)

    def _item_boxes(self):
        y = self.pad
        out = []
        for it in self.items:
            h = self.sep_h if it["type"] == "sep" else self.item_h
            out.append((y, y + h))
            y += h
        return out

    # ---- 显示 ----
    def show_at(self, x, y):
        x, y = int(x), int(y)
        vx = ctypes.windll.user32.GetSystemMetrics(76)
        vy = ctypes.windll.user32.GetSystemMetrics(77)
        vw = ctypes.windll.user32.GetSystemMetrics(78)
        vh = ctypes.windll.user32.GetSystemMetrics(79)
        # 靠近屏幕边缘时朝反方向展开, 避免盖住点击位置
        if x + self.width > vx + vw - 4:
            x = x - self.width
        if y + self.height > vy + vh - 4:
            y = y - self.height
        x = max(vx + 4, min(x, vx + vw - self.width - 4))
        y = max(vy + 4, min(y, vy + vh - self.height - 4))
        self.geometry(f"{self.width}x{self.height}+{x}+{y}")
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        force_topmost(self)
        if self.parent_menu is None:
            self.focus_force()
            focus_window(self)     # OS 层焦点, Esc 等按键才生效
            self._watch_clicks()
            self._watch_hover()
        return self

    # ---- 绘制 ----
    def _draw(self):
        c = self.canvas
        c.delete("all")
        w, h = self.width, self.height
        draw_round_rect(c, 0.5, 0.5, w - 0.5, h - 0.5, 10 * self.scale,
                        self.pal["menu_bg"], self.pal["menu_border"], 1)
        for idx, (it, (y1, y2)) in enumerate(zip(self.items, self._item_boxes())):
            if it["type"] == "sep":
                c.create_line(self.pad + 8 * self.scale, (y1 + y2) / 2,
                              w - self.pad - 8 * self.scale, (y1 + y2) / 2,
                              fill=self.pal["border_soft"])
                continue
            if idx == self._hover:
                draw_round_rect(c, 4 * self.scale, y1 + 2, w - 4 * self.scale, y2 - 2,
                                7 * self.scale, self.pal["menu_hover"], "", 1)
            color = self.pal["pos"] if it.get("danger") else self.pal["text"]
            x = self.pad + 10 * self.scale
            if it["type"] == "check":
                if it.get("checked"):
                    c.create_text(x + 2 * self.scale, (y1 + y2) / 2,
                                  text="✓", anchor="w", fill=self.pal["accent"],
                                  font=self.fonts.ui(-1, True))
                x += 18 * self.scale
            elif it["type"] == "sub":
                pass
            c.create_text(x, (y1 + y2) / 2 - 0.5, text=it.get("label", ""), anchor="w",
                          fill=color, font=self.font_item)
            if it["type"] == "sub":
                c.create_text(w - self.pad - 10 * self.scale, (y1 + y2) / 2 - 0.5,
                              text="›", anchor="e", fill=self.pal["muted"],
                              font=self.fonts.ui(1))
            elif it.get("shortcut"):
                c.create_text(w - self.pad - 12 * self.scale, (y1 + y2) / 2 - 0.5,
                              text=it["shortcut"], anchor="e", fill=self.pal["faint"],
                              font=self.font_hint)

    # ---- 交互 ----
    def _index_at(self, y):
        for idx, (y1, y2) in enumerate(self._item_boxes()):
            if y1 <= y < y2:
                return idx if self.items[idx]["type"] != "sep" else None
        return None

    def _on_motion(self, event):
        idx = self._index_at(event.y)
        if idx != self._hover:
            self._hover = idx
            self._draw()
            if self._sub_after:
                try:
                    self.after_cancel(self._sub_after)
                except tk.TclError:
                    pass
                self._sub_after = None
            if idx is not None and self.items[idx]["type"] == "sub":
                self._sub_after = self.after(130, lambda i=idx: self._open_sub(i))

    def _on_leave(self, _event):
        if self._sub_after:
            try:
                self.after_cancel(self._sub_after)
            except tk.TclError:
                pass
            self._sub_after = None
        if self._hover is not None:
            self._hover = None
            self._draw()

    def _open_sub(self, idx):
        self._sub_after = None
        if not self.winfo_exists():
            return
        for child in list(self.submenus):
            self._close_child(child)
        it = self.items[idx]
        y1, y2 = self._item_boxes()[idx]
        child = MenuPopup(self.master, it["items"], self.pal, self.fonts,
                          scale=self.scale, parent_menu=self)
        self.submenus.append(child)
        child.update_idletasks()
        w = self.width
        u = ctypes.windll.user32
        vx, vw = u.GetSystemMetrics(76), u.GetSystemMetrics(78)
        x = self.winfo_rootx() + w - 4
        if x + child.width > vx + vw - 4:
            x = self.winfo_rootx() - child.width + 4
        y = self.winfo_rooty() + y1 - self.pad
        child.show_at(x, y)
        child.lift()

    def _on_click(self, event):
        idx = self._index_at(event.y)
        if idx is None:
            return
        it = self.items[idx]
        if it["type"] == "sub":
            self._open_sub(idx)
            return
        if it["type"] in ("cmd", "check"):
            cb = it.get("command")
            master = self.master
            self.close_root()
            if callable(cb):
                master.after(10, cb)

    @staticmethod
    def _mouse_down() -> bool:
        try:
            u = ctypes.windll.user32
            return bool(u.GetAsyncKeyState(0x01) & 0x8000 or u.GetAsyncKeyState(0x02) & 0x8000)
        except Exception:
            return False

    def _watch_clicks(self):
        if self._closed:
            return
        try:
            down = self._mouse_down()
            if down and not self._press_was_down:
                px, py = self.winfo_pointerxy()
                inside = False
                for menu in self._all_menus():
                    try:
                        mx, my = menu.winfo_rootx(), menu.winfo_rooty()
                        if mx <= px <= mx + menu.width and my <= py <= my + menu.height:
                            inside = True
                            break
                    except tk.TclError:
                        continue
                if not inside:
                    self.close_root()
                    return
            self._press_was_down = down
            self.after(70, self._watch_clicks)
        except tk.TclError:
            pass

    def _watch_hover(self):
        """鼠标离开菜单（含子菜单）超过 3 秒自动收起; 在菜单内则一直保留。"""
        if self._closed:
            return
        try:
            px, py = self.winfo_pointerxy()
            inside = False
            for menu in self._all_menus():
                try:
                    mx, my = menu.winfo_rootx(), menu.winfo_rooty()
                    if mx <= px <= mx + menu.width and my <= py <= my + menu.height:
                        inside = True
                        break
                except tk.TclError:
                    continue
            if inside:
                self._outside_since = None
            else:
                now = time.time()
                if self._outside_since is None:
                    self._outside_since = now
                elif now - self._outside_since >= 3.0:
                    self.close_root()
                    return
            self.after(100, self._watch_hover)
        except tk.TclError:
            pass

    def _all_menus(self):
        root = self
        while root.parent_menu is not None:
            root = root.parent_menu
        out, stack = [], [root]
        while stack:
            m = stack.pop()
            out.append(m)
            stack.extend(m.submenus)
        return out

    def _close_child(self, child):
        try:
            child.close_root()
        except Exception:
            pass
        if child in self.submenus:
            self.submenus.remove(child)

    def close_root(self):
        root = self
        while root.parent_menu is not None:
            root = root.parent_menu
        root._close_tree()

    def _close_tree(self):
        if self._closed:
            return
        self._closed = True
        for child in list(self.submenus):
            child._close_tree()
        self.submenus.clear()
        if self in MenuPopup._open_menus:
            MenuPopup._open_menus.remove(self)
        cb = self.on_close if self.parent_menu is None else None
        try:
            self.destroy()
        except tk.TclError:
            pass
        if callable(cb):
            cb()

    def destroy(self):
        if not self._closed:
            self._closed = True
            for child in list(self.submenus):
                child._close_tree()
            self.submenus.clear()
            if self in MenuPopup._open_menus:
                MenuPopup._open_menus.remove(self)
        super().destroy()


# ---------------- 深色菜单/表格辅助 ----------------
def setup_ttk(root, pal, fonts, scale=1.0):
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    row_h = int(28 * scale)
    style.configure("SB.Treeview", background=pal["panel_soft"], fieldbackground=pal["panel_soft"],
                    foreground=pal["text"], rowheight=row_h, borderwidth=0,
                    font=(UI_FAMILY, fonts.base - 2))
    style.configure("SB.Treeview.Heading", background=pal["panel_alt"], foreground=pal["muted"],
                    relief="flat", borderwidth=0, padding=(8, 6))
    style.map("SB.Treeview.Heading", background=[("active", pal["hover"])])
    style.map("SB.Treeview", background=[("selected", pal["selected"])],
              foreground=[("selected", pal["text"])])
    style.layout("SB.Treeview", [("SB.Treeview.treearea", {"sticky": "nswe"})])
    style.configure("SB.Vertical.TScrollbar", gripcount=0, background=pal["scroll"],
                    troughcolor=pal["panel_soft"], bordercolor=pal["panel_soft"],
                    arrowcolor=pal["faint"], width=int(9 * scale))
    style.map("SB.Vertical.TScrollbar", background=[("active", pal["muted"])])
    style.configure("SB.TCombobox", fieldbackground=pal["field"], background=pal["field"],
                    foreground=pal["text"], arrowcolor=pal["muted"],
                    bordercolor=pal["field_border"], lightcolor=pal["field"],
                    darkcolor=pal["field"])
    return style


def make_entry(parent, pal, fonts, width=14, textvariable=None, font_delta=-2, numeric=False):
    font = fonts.num(font_delta, False) if numeric else fonts.ui(font_delta)
    entry = tk.Entry(parent, width=width, textvariable=textvariable,
                     bg=pal["field"], fg=pal["text"], insertbackground=pal["text"],
                     relief="flat", bd=0, highlightthickness=1,
                     highlightbackground=pal["field_border"],
                     highlightcolor=pal["accent"],
                     font=font, disabledbackground=pal["field"],
                     disabledforeground=pal["faint"])
    return entry
