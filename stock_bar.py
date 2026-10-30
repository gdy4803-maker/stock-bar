# -*- coding: utf-8 -*-
"""沪深实时行情桌面导航栏 (StockBar) v1.2
============================================
一个极简的 Windows 桌面行情条: 胶囊样式, 悬浮在桌面/任务栏上, 只显示涨跌幅。
  - 左边: 当前自选股涨跌幅(点击/方向键切换)
  - 中间: 持仓当日盈亏(有持仓时显示)
  - 右边: 指数涨跌幅
  - 悬停: 弹出详情卡(名称/现价/涨跌/分时图/OHLC/量额/持仓盈亏)
  - 右键: 深色菜单(设置/自选/持仓/切换/刷新间隔/置顶/退出)

技术要点:
  - 双窗口: 视觉窗口(色键透明, 只画文字/胶囊) + 交互窗口(几乎全透明, 整块命中),
    保证"背景透明"的同时, 悬停/点击/拖动都容易命中。
  - 全部 UI 由 tkinter Canvas 自绘, 只依赖标准库, 可打包为单文件 exe。
  - 支持多显示器、高 DPI、深浅主题、红涨绿跌/绿涨红跌、开机自启。

数据源: 腾讯行情接口 https://qt.gtimg.cn/ (免费、低延迟, A股约3秒/笔)
用法:
    python stock_bar.py          启动
    python stock_bar.py --check  自检: 拉取一次行情并打印
    python stock_bar.py --help   查看帮助
配置: 同目录 config.json (首次运行自动生成); 日志: stock_bar.log
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import unicodedata
from ctypes import wintypes

try:
    import winreg
except ImportError:  # pragma: no cover - 仅 Windows
    winreg = None

import sb_data as D
import sb_ui
from sb_theme import (Fonts, KEY_COLOR, Screen, darken, draw_round_rect,
                      enable_dpi_awareness, focus_window, force_topmost,
                      insert_below, lighten, mix, palette, sample_screen_rect,
                      set_topmost, set_window_alpha)
from sb_widgets import MenuPopup

APP_NAME = "沪深行情导航栏"
APP_VERSION = "1.3.6"
REPO_URL = "https://github.com/gdy4803-maker/stock-bar"

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_PATH = os.path.join(BASE_DIR, "stock_bar.log")
LOCK_PATH = os.path.join(BASE_DIR, "stock_bar.lock")

INTERVALS = (0.5, 1, 2, 3, 5, 10)

DEFAULT_CONFIG = {
    "stocks": [
        "sh600519", "sz300750", "sz002594", "sh688981", "sz000858", "sh601318",
    ],
    "index": "sh000001",
    "interval_seconds": 2,
    "opacity": 0.92,
    "bg_opacity": 1.0,        # 胶囊背景不透明度; 0 = 只显示文字(透明背景)
    "topmost": True,
    "font_size": 12,
    "window_pos": None,
    "holdings": {},
    "holdings_layout": {},           # 持仓窗口布局记忆(尺寸/两个列表高度)
    "fees": dict(D.DEFAULT_FEES),   # 交易手续费: 佣金费率/起收/印花税
    "appearance": "pill",     # pill=胶囊(带名称) | compact=紧凑(仅数字)
    "theme": "dark",          # dark | light
    "show_name": True,        # 胶囊里是否显示股票名称
    "cn_colors": True,        # True=红涨绿跌, False=绿涨红跌
    "card_mode": "detail",    # detail=详细(含分时图) | simple=简洁(纯文字)
    "card_opacity": 1.0,      # 悬停卡片透明度 0.10 ~ 1.0
    "pnl_display": "money",   # 行情条持仓段显示: money=当日盈亏金额 | pct=涨幅
    "dock_taskbar": False,    # True=嵌入任务栏(左), 拖动只在任务栏内滑动
}

INPUT_BG = "#0B0C10"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "StockBar"
STARTUP_DIR = os.path.join(os.environ.get("APPDATA", ""),
                           r"Microsoft\Windows\Start Menu\Programs\Startup")
STARTUP_FILE = os.path.join(STARTUP_DIR, "StockBar.vbs")
STARTUP_COMMON_DIR = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                                  r"Microsoft\Windows\Start Menu\Programs\StartUp")
STARTUP_COMMON_FILE = os.path.join(STARTUP_COMMON_DIR, "StockBar.vbs")
STARTUP_FILES = (STARTUP_FILE, STARTUP_COMMON_FILE)


# ---------------- 配置 ----------------
def _default_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg["stocks"] = list(DEFAULT_CONFIG["stocks"])
    cfg["holdings"] = {}
    cfg["fees"] = dict(DEFAULT_CONFIG["fees"])
    return cfg


def normalize_config(raw: dict | None) -> dict:
    cfg = _default_config()
    if isinstance(raw, dict):
        cfg.update(raw)
    # 自选股
    stocks, seen = [], set()
    for code in cfg.get("stocks") or []:
        if not isinstance(code, str):
            continue
        code = D.normalize_code(code)
        if code and code not in seen:
            seen.add(code)
            stocks.append(code)
    cfg["stocks"] = stocks[:40]
    cfg["index"] = D.normalize_code(str(cfg.get("index") or "sh000001"), as_index=True)
    try:
        interval = float(cfg.get("interval_seconds", 2))
    except (TypeError, ValueError):
        interval = 2.0
    if interval not in INTERVALS:
        interval = min(INTERVALS, key=lambda v: abs(v - interval))
    cfg["interval_seconds"] = interval
    try:
        cfg["opacity"] = max(0.10, min(1.0, float(cfg.get("opacity", 0.92))))
    except (TypeError, ValueError):
        cfg["opacity"] = 0.92
    if raw is None:
        bg = 1.0                            # 全新配置: 背景默认不透明
    elif isinstance(raw, dict) and "bg_opacity" in raw:
        try:
            bg = float(cfg.get("bg_opacity", 1.0))
        except (TypeError, ValueError):
            bg = 1.0
    else:
        bg = cfg["opacity"]                 # 旧配置迁移: 原整体不透明度视为背景
    cfg["bg_opacity"] = max(0.0, min(1.0, bg))
    try:
        cfg["font_size"] = max(10, min(16, int(cfg.get("font_size", 12))))
    except (TypeError, ValueError):
        cfg["font_size"] = 12
    cfg["topmost"] = bool(cfg.get("topmost", True))
    cfg["show_name"] = bool(cfg.get("show_name", True))
    cfg["cn_colors"] = bool(cfg.get("cn_colors", True))
    if cfg.get("appearance") == "minimal":
        cfg["appearance"] = "compact"
    if cfg.get("appearance") not in ("pill", "compact"):
        cfg["appearance"] = "pill"
    if cfg.get("card_mode") not in ("detail", "simple"):
        cfg["card_mode"] = "detail"
    try:
        card_op = float(cfg.get("card_opacity", 1.0))
    except (TypeError, ValueError):
        card_op = 1.0
    cfg["card_opacity"] = max(0.10, min(1.0, card_op))
    if cfg.get("pnl_display") not in ("money", "pct"):
        cfg["pnl_display"] = "money"
    if cfg.get("theme") not in ("dark", "light"):
        cfg["theme"] = "dark"
    cfg["dock_taskbar"] = bool(cfg.get("dock_taskbar", False))
    pos = cfg.get("window_pos")
    if not (isinstance(pos, dict) and isinstance(pos.get("x"), (int, float))
            and isinstance(pos.get("y"), (int, float))):
        cfg["window_pos"] = None
    holdings = {}
    for code, h in (cfg.get("holdings") or {}).items():
        if not isinstance(code, str) or not isinstance(h, dict):
            continue
        code = D.normalize_code(code)
        if not code:
            continue
        trades = []
        raw_trades = h.get("trades")
        if isinstance(raw_trades, list):
            for t in raw_trades:
                if not isinstance(t, dict):
                    continue
                side = t.get("side")
                if side not in ("buy", "sell"):
                    continue
                try:
                    price = float(t.get("price") or 0)
                    lots = int(t.get("lots") or 0)
                    fee = float(t.get("fee") or 0)
                except (TypeError, ValueError):
                    continue
                if lots <= 0 or price < 0:
                    continue
                trades.append({
                    "time": str(t.get("time") or "")[:24],
                    "side": side,
                    "price": round(price, 4),
                    "lots": lots,
                    "fee": round(max(0.0, fee), 2),
                    "note": str(t.get("note") or "")[:40],
                })
        else:
            # 旧格式(手数+成本价)迁移为一条"期初持仓"买入记录
            try:
                lots = int(h.get("lots", 0))
            except (TypeError, ValueError):
                lots = 0
            try:
                cost = float(h.get("cost")) if h.get("cost") else 0.0
            except (TypeError, ValueError):
                cost = 0.0
            if lots > 0:
                trades.append({"time": "期初", "side": "buy",
                               "price": round(max(0.0, cost), 4),
                               "lots": lots, "fee": 0.0, "note": "期初持仓"})
        if trades:
            holdings[code] = {"trades": trades}
    cfg["holdings"] = holdings
    # 交易手续费(可配置): 缺项/非法值回落默认
    fees = dict(D.DEFAULT_FEES)
    for k, v in (cfg.get("fees") or {}).items():
        if k in fees:
            try:
                fv = float(v)
                if fv >= 0:
                    fees[k] = fv
            except (TypeError, ValueError):
                pass
    cfg["fees"] = fees
    # 持仓窗口布局记忆
    lay = cfg.get("holdings_layout")
    clean_lay = {}
    if isinstance(lay, dict):
        for k in ("w", "h", "h_top", "h_log"):
            try:
                v = int(lay.get(k) or 0)
                if v > 0:
                    clean_lay[k] = v
            except (TypeError, ValueError):
                pass
    cfg["holdings_layout"] = clean_lay
    return cfg


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                return normalize_config(json.load(f))
        except Exception:
            try:
                os.replace(CONFIG_PATH, CONFIG_PATH + ".bak")
            except OSError:
                pass
    return normalize_config(None)


def save_config(cfg: dict) -> bool:
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        return True
    except OSError:
        return False


def _short_name(name: str, limit: int = 4) -> str:
    """名称截断到 limit 个字符; 不切断连续的字母/数字。"""
    n = (name or "").strip()
    if not n or len(n) <= limit:
        return n
    cut = n[:limit]
    if n[limit - 1].isascii() and n[limit - 1].isalnum():
        j = limit
        while j < len(n) and n[j].isascii() and n[j].isalnum():
            j += 1
        return n[:j]
    return cut


def _index_short(name: str) -> str:
    n = (name or "").strip()
    for key, short in (("上证指数", "上证"), ("深证成指", "深成"), ("创业板指", "创业板"),
                       ("科创50", "科创50"), ("北证50", "北证50"), ("沪深300", "沪深300"),
                       ("中证500", "中证500"), ("中证1000", "中证1000")):
        if n.startswith(key):
            return short
    return _short_name(n, 4) or "指数"


# ---------------- 开机自启 ----------------
TASK_NAME = "StockBar"


def _autostart_parts() -> tuple[str, str]:
    """返回 (程序路径, 参数)。"""
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable), ""
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        cand = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(cand):
            exe = cand
    return exe, f'"{os.path.join(BASE_DIR, "stock_bar.py")}"'


def _autostart_command() -> str:
    exe, args = _autostart_parts()
    return f'"{exe}" {args}'.rstrip()


def _startup_vbs() -> str:
    """生成登录时静默启动的 VBS(注册表不可写时的备用方案)。"""
    cmd = _autostart_command()
    arg = '"' + cmd.replace('"', '""') + '"'
    return 'CreateObject("WScript.Shell").Run ' + arg + ', 0, False\r\n'


def _write_startup_vbs() -> bool:
    errors = []
    for path in STARTUP_FILES:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="mbcs", newline="\r\n") as f:
                f.write(_startup_vbs())
            return True
        except OSError as exc:
            errors.append(f"{path}: {exc!r}")
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") +
                    "写入启动文件夹失败: " + " | ".join(errors) + "\n")
    except OSError:
        pass
    return False


def _remove_startup_vbs() -> None:
    for path in STARTUP_FILES:
        try:
            os.remove(path)
        except OSError:
            pass


def _sched_task_exists() -> bool:
    """计划任务方式的开机自启是否已注册。"""
    try:
        p = subprocess.run(["schtasks", "/query", "/tn", TASK_NAME],
                           capture_output=True, timeout=10)
        return p.returncode == 0
    except Exception:
        return False


def _write_sched_task() -> bool:
    """用计划任务(登录触发)注册开机自启 —— 注册表/启动文件夹被安全软件拦截时的第三通道。

    当前用户可在不提权的情况下注册自己的登录任务(PowerShell TaskScheduler cmdlet)。
    """
    exe, args = _autostart_parts()

    def q(s: str) -> str:
        return s.replace("'", "''")

    ps = (
        "$a=New-ScheduledTaskAction -Execute '%s' -Argument '%s';"
        "$t=New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME;"
        "Register-ScheduledTask -TaskName '%s' -Action $a -Trigger $t "
        "-RunLevel Limited -Force | Out-Null"
    ) % (q(exe), q(args), q(TASK_NAME))
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, timeout=40)
        ok = p.returncode == 0 and _sched_task_exists()
    except Exception:
        ok = False
    if not ok:
        try:
            err = (p.stderr or b"").decode("mbcs", "ignore").strip()
        except Exception:
            err = "?"
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") +
                        f"计划任务自启注册失败: {err}\n")
        except OSError:
            pass
    return ok


def _remove_sched_task() -> None:
    try:
        subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
                       capture_output=True, timeout=20)
    except Exception:
        pass
    if _sched_task_exists():
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false"],
                capture_output=True, timeout=30)
        except Exception:
            pass


def is_autostart() -> bool:
    if any(os.path.exists(p) for p in STARTUP_FILES):
        return True
    if winreg is not None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, RUN_VALUE)
            return True
        except Exception:
            pass
    return _sched_task_exists()


def set_autostart(enabled: bool) -> bool:
    """依次尝试: 注册表 Run -> 启动文件夹 VBS -> 计划任务。

    前两条容易被安全软件(如火绒"启动项防护")拦截, 计划任务是另一条通道。
    """
    if not enabled:
        if winreg is not None:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                    winreg.KEY_SET_VALUE) as key:
                    try:
                        winreg.DeleteValue(key, RUN_VALUE)
                    except FileNotFoundError:
                        pass
            except Exception:
                pass
        _remove_startup_vbs()
        _remove_sched_task()
        return not is_autostart()

    errors = []
    # 1) 注册表
    if winreg is not None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ,
                                  _autostart_command())
            _remove_startup_vbs()
            _remove_sched_task()
            return True
        except Exception as exc:
            errors.append(f"注册表: {exc!r}")
    # 2) 启动文件夹
    if _write_startup_vbs():
        _remove_sched_task()
        return True
    errors.append("启动文件夹: 被拒绝")
    # 3) 计划任务
    if _write_sched_task():
        return True
    errors.append("计划任务: 被拒绝")
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") +
                    "写入开机自启失败(三种方式): " + " | ".join(errors) + "\n")
    except OSError:
        pass
    return False


# ---------------- 单实例 ----------------
def _pid_alive(pid: int) -> bool:
    try:
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        code = wintypes.DWORD()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259
    except Exception:
        return False


def _pid_image(pid: int) -> str:
    try:
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return ""
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        ctypes.windll.kernel32.CloseHandle(h)
        return buf.value if ok else ""
    except Exception:
        return ""


def _instance_pid() -> int:
    """已有实例返回其 PID, 否则返回 0。"""
    try:
        with open(LOCK_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        pid = int(data.get("pid", 0))
        exe = str(data.get("exe", ""))
    except Exception:
        return 0
    if pid <= 0 or not _pid_alive(pid):
        return 0
    running = os.path.basename(_pid_image(pid)).lower()
    mine = os.path.basename(sys.executable).lower()
    if running and mine and running != mine:
        return 0
    return pid


def _write_lock() -> None:
    try:
        with open(LOCK_PATH, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "exe": os.path.abspath(sys.executable),
                       "started": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    except OSError:
        pass


def _remove_lock() -> None:
    try:
        if _instance_pid() == os.getpid():
            os.remove(LOCK_PATH)
    except OSError:
        pass


def _flash_existing() -> None:
    """让已在运行的窗口闪一下并置前。"""
    try:
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        hwnd = user32.FindWindowW(None, APP_NAME)
        if not hwnd:
            return
        user32.SetForegroundWindow(hwnd)
        for _ in range(3):
            user32.ShowWindow(hwnd, 0)
            time.sleep(0.16)
            user32.ShowWindow(hwnd, 8)
            time.sleep(0.16)
    except Exception:
        pass


# ---------------- 主程序 ----------------
class StockBar:
    INTERVALS = INTERVALS

    def __init__(self, cfg: dict):
        self.version = APP_VERSION
        self.repo_url = REPO_URL
        self.base_dir = BASE_DIR
        self.cfg = normalize_config(cfg)
        self.screen = Screen()
        self.quotes: dict[str, D.Quote] = {}
        self.pick_codes: list[str] = []
        self.pick_index = 0
        self.index_code = ""
        self.pal = palette(self.cfg["theme"], self.cfg["cn_colors"])
        self._dialogs: dict[str, object] = {}
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._gen = 0
        self._last_msg_at = time.time()      # 看门狗: 最近一次收到取数线程消息的时间
        self._minute_cache: dict[str, tuple[float, D.MinuteSeries | None]] = {}
        self._minute_pending: set[str] = set()
        self._minute_waiters: dict[str, list] = {}
        self._menu: MenuPopup | None = None
        self._card = None
        self._drag = None
        self._pressed = False
        self._hovering = False
        self._hover_zone = "pick"
        self._hover_after = None
        self._error = False
        self._last_vals: dict[str, float | None] = {}
        self._flash: dict[str, float] = {}
        self._x = self._y = 0
        self._w = self._h = 0
        self._first_place = True
        self._tick = 0
        self._wall_cache = None      # (采样时间, 壁纸采样色)
        self._text_key = None        # 文字层色键 = 文字背后合成色
        self._bg_alpha_on = None     # 已应用的层透明度(避免无谓的属性设置)
        self._text_alpha_on = None
        self._topmost_on = None

        self.root = tk.Tk()
        self.root.report_callback_exception = self._tk_callback_error
        self.root.title(APP_NAME)
        self.root.overrideredirect(True)
        # 文字层: 色键 = 胶囊底色, ClearType 会混到底色上, 文字锐利无彩边
        self.root.configure(bg=self.pal["bar_bg"])
        try:
            self.root.tk.call("tk", "scaling", self.screen.dpi / 72.0)
        except tk.TclError:
            pass
        self.scale = max(0.75, min(3.0, self.screen.dpi / 96.0))
        self.fonts = Fonts(self.root, self.cfg["font_size"])
        self.ui_fonts = Fonts(self.root, 12)
        self.card_fonts = Fonts(self.root, 11)
        try:
            self.root.attributes("-transparentcolor", self.pal["bar_bg"])
        except tk.TclError:
            pass
        self.root.attributes("-alpha", self.cfg["opacity"])
        self.root.attributes("-topmost", self.cfg["topmost"])
        self._make_click_through(self.root)

        self.canvas = tk.Canvas(self.root, bg=self.pal["bar_bg"], highlightthickness=0, bd=0,
                                width=10, height=10)
        self.canvas.place(x=0, y=0, width=10, height=10)

        # 背景层: 只画胶囊底色, 单独控制不透明度(0 = 纯文字)
        self.bg = tk.Toplevel(self.root)
        self.bg.overrideredirect(True)
        self.bg.configure(bg=KEY_COLOR)
        self.bg.attributes("-topmost", self.cfg["topmost"])
        try:
            self.bg.attributes("-transparentcolor", KEY_COLOR)
            self.bg.attributes("-alpha", self.cfg["bg_opacity"])
        except tk.TclError:
            pass
        self._make_click_through(self.bg)
        self.bg_canvas = tk.Canvas(self.bg, bg=KEY_COLOR, highlightthickness=0, bd=0,
                                   width=10, height=10)
        self.bg_canvas.place(x=0, y=0, width=10, height=10)

        self.input = tk.Toplevel(self.root)
        self.input.overrideredirect(True)
        self.input.configure(bg=INPUT_BG)
        self.input.attributes("-topmost", self.cfg["topmost"])
        try:
            self.input.attributes("-alpha", 0.02)   # 先建立分层窗口
        except tk.TclError:
            pass
        # 压到 1/255: 肉眼不可见, 但仍参与鼠标命中 (alpha=0 会失去命中)
        set_window_alpha(self.input, 1)
        self.input_frame = tk.Frame(self.input, bg=INPUT_BG)
        self.input_frame.pack(fill="both", expand=True)

        for w in (self.input, self.input_frame):
            w.bind("<Button-1>", self._on_press)
            w.bind("<B1-Motion>", self._on_drag)
            w.bind("<ButtonRelease-1>", self._on_release)
            w.bind("<Button-3>", self._on_menu)
        self.input.bind("<Left>", lambda e: self._switch(-1))
        self.input.bind("<Right>", lambda e: self._switch(1))
        self.input.bind("<Up>", lambda e: self._switch(-1))
        self.input.bind("<Down>", lambda e: self._switch(1))
        self.input.bind("<Escape>", lambda e: self._hide_card())
        self.input.bind("<Control-comma>", lambda e: self.open_settings())
        self.input.bind("<Control-m>", lambda e: self.open_manage())
        self.input.bind("<Control-h>", lambda e: self.open_holdings())
        self.input.bind("<Control-c>", lambda e: self.copy_quotes())
        self.input.bind("<F5>", lambda e: self.reload_config())

        self._load_lists()
        self._card = sb_ui.InfoCard(self)
        self._render_bar(force=True)
        self._restack_bar()
        self._restart_worker()
        self._after_queue = self.root.after(100, self._poll_queue)
        self._after_mouse = self.root.after(100, self._poll_mouse)
        self.root.after(60, self._startup_pulse)

    # ---------- 基础 ----------
    def _make_click_through(self, win) -> None:
        try:
            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            user32 = ctypes.windll.user32
            user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
            user32.SetWindowLongW.restype = ctypes.c_long
            user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.GetWindowLongW.restype = ctypes.c_long
            user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.GetAncestor.restype = ctypes.c_void_p
            child = win.winfo_id()
            frame = user32.GetAncestor(child, 2) or 0   # GA_ROOT: 真正的顶层框架
            for hwnd in (child, frame):
                if not hwnd:
                    continue
                style = user32.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
                user32.SetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE,
                                      style | WS_EX_TRANSPARENT)
        except Exception:
            pass

    def _log(self, msg: str) -> None:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
        except OSError:
            pass

    def _load_lists(self) -> None:
        self.pick_codes = [D.normalize_code(c) for c in self.cfg.get("stocks", []) if c]
        if self.pick_index >= len(self.pick_codes):
            self.pick_index = 0
        self.index_code = D.normalize_code(str(self.cfg.get("index", "sh000001")), as_index=True)

    def _current_pick_code(self) -> str:
        if not self.pick_codes:
            return ""
        return self.pick_codes[self.pick_index % len(self.pick_codes)]

    def bar_rect(self):
        return self._x, self._y, max(1, self._w), max(1, self._h)

    # ---------- 渲染 ----------
    def _text_palette(self):
        """文字配色随背景明暗自适应, 任意透明度/壁纸下都保持可读。"""
        key = self._composite_key()
        try:
            lum = (0.299 * int(key[1:3], 16) + 0.587 * int(key[3:5], 16)
                   + 0.114 * int(key[5:7], 16))
        except Exception:
            lum = 0.0
        return palette("light" if lum >= 148 else "dark", bool(self.cfg.get("cn_colors", True)))

    def _color(self, pct, pal=None) -> str:
        pal = pal or self.pal
        if pct is None:
            return pal["flat"]
        if pct > 0:
            return pal["pos"]
        if pct < 0:
            return pal["neg"]
        return pal["flat"]

    def _flash_color(self, role: str, color: str, pal=None) -> str:
        pal = pal or self.pal
        until = self._flash.get(role, 0)
        if until > time.time():
            # 浅色背景用压暗, 深色背景用提亮, 保证闪烁可见
            return darken(color, 0.30) if pal.get("name") == "light" else lighten(color, 0.42)
        return color

    def _pct_of(self, code: str) -> float | None:
        q = self.quotes.get(code)
        return q.pct if q else None

    def _bar_parts(self):
        pal, F, S = self._text_palette(), self.fonts, self.scale
        mode = self.cfg["appearance"]
        pick = self.quotes.get(self._current_pick_code())
        idx = self.quotes.get(self.index_code)
        pnl = self._pnl_display_value()
        parts = []

        def add(text, font, color, gap=0, kind="text"):
            parts.append({"text": text, "font": font, "color": color,
                          "gap": gap * S, "kind": kind})

        pick_pct = pick.pct if pick else None
        pick_color = self._flash_color("pick", self._color(pick_pct, pal), pal)
        pick_text = D.fmt_pct(pick_pct)
        idx_pct = idx.pct if idx else None
        idx_color = self._flash_color("index", self._color(idx_pct, pal), pal)
        idx_text = D.fmt_pct(idx_pct)
        pnl_color = self._flash_color("pnl", self._color(pnl, pal), pal)
        if self.cfg.get("pnl_display", "money") == "pct":
            pnl_text = D.fmt_pct(pnl) if pnl is not None else "--"
        else:
            pnl_text = D.fmt_money_signed(pnl) if pnl is not None else "--"
        name = _short_name(pick.name, 4) if pick else ""

        if mode == "compact":
            add(pick_text, F.num(), pick_color, 0, "pick")
            if pnl is not None:
                add("", F.label(), pal["border"], 9, "sep")
                add(pnl_text, F.num(), pnl_color, 9, "pnl")
            if self.index_code:
                add("", F.label(), pal["border"], 9, "sep")
                add(_index_short(idx.name) if idx else "指数", F.label(), pal["muted"], 9, "idxname")
                add(idx_text, F.num(), idx_color, 4, "index")
            return parts, 11 * S, 0

        # 胶囊
        if self.cfg["show_name"] and name:
            add(name, F.name(), pal["text_dim"], 0, "name")
            add(pick_text, F.num(), pick_color, 6, "pick")
        else:
            add(pick_text, F.num(), pick_color, 0, "pick")
        if pnl is not None:
            add("", F.label(), pal["border"], 9, "sep")
            add("持仓", F.label(), pal["muted"], 9, "pnlname")
            add(pnl_text, F.num(), pnl_color, 4, "pnl")
        if self.index_code:
            add("", F.label(), pal["border"], 9, "sep")
            add(_index_short(idx.name) if idx else "指数", F.label(), pal["muted"], 9, "idxname")
            add(idx_text, F.num(), idx_color, 4, "index")
        return parts, 12 * S, 0

    def _effective_bg_op(self) -> float:
        """实际生效的背景不透明度: 嵌入任务栏时不画胶囊底色。"""
        if self.cfg.get("dock_taskbar"):
            return 0.0
        return max(0.0, min(1.0, float(self.cfg.get("bg_opacity", 1.0))))

    # ---------- 文字层色键: 与背后合成色一致, 抗锯齿无彩边 ----------
    def _wall_color(self):
        """采样行情条四周一圈的壁纸颜色(避开自身与详情卡), 带缓存。"""
        now = time.time()
        if self._wall_cache and now - self._wall_cache[0] < 20:
            return self._wall_cache[1]
        if self._card is not None and self._card.visible:
            # 详情卡可能盖住采样区, 沿用旧值
            return self._wall_cache[1] if self._wall_cache else None
        x, y, w, h = self._x, self._y, max(1, self._w), max(1, self._h)
        s, t = 6, 4   # 离窗口 6px, 采样条厚 4px
        regions = ((x, y - s - t, w, t), (x, y + h + s, w, t),
                   (x - s - t, y, t, h), (x + w + s, y, t, h))
        rs = gs = bs = n = 0
        for rx, ry, rw, rh in regions:
            # 裁剪到虚拟屏幕内: 屏幕外会采到黑边, 把合成色算偏
            x0 = max(int(rx), self.screen.vx)
            y0 = max(int(ry), self.screen.vy)
            x1 = min(int(rx + rw), self.screen.vx + self.screen.vw)
            y1 = min(int(ry + rh), self.screen.vy + self.screen.vh)
            if x1 - x0 < 2 or y1 - y0 < 2:
                continue
            color = sample_screen_rect(x0, y0, x1 - x0, y1 - y0)
            if color:
                rs += color[0]
                gs += color[1]
                bs += color[2]
                n += 1
        if not n:
            return self._wall_cache[1] if self._wall_cache else None
        wall = "#%02X%02X%02X" % (rs // n, gs // n, bs // n)
        self._wall_cache = (now, wall)
        return wall

    def _composite_key(self):
        """文字背后的合成色: 胶囊底色按透明度混入壁纸。"""
        bg_op = self._effective_bg_op()
        if bg_op >= 0.995:
            return self.pal["bar_bg"]
        wall = self._wall_color()
        if wall is None:
            return self.pal["bar_bg"]
        return mix(self.pal["bar_bg"], wall, max(0.0, 1.0 - bg_op))

    def _apply_text_key(self) -> bool:
        key = self._composite_key()
        if key == self._text_key:
            return False
        self._text_key = key
        try:
            self.root.configure(bg=key)
            self.canvas.configure(bg=key)
            self.root.attributes("-transparentcolor", key)
        except tk.TclError:
            pass
        return True

    def _restack_bar(self):
        """维持层级: 胶囊层 -> 文字层 -> 交互层 -> 卡片/菜单/对话框。

        设置窗口属性(Tk 内部会重新布置窗口)会打乱 z 序,
        交互层被压到下面会导致鼠标点击被背景层吞掉。
        """
        try:
            if self.cfg.get("topmost", True):
                force_topmost(self.bg)
                force_topmost(self.root)
                force_topmost(self.input)
            else:
                # 必须清掉顶层框架的 WS_EX_TOPMOST(Tk 的 -topmost 管不到),
                # 否则"窗口置顶"开关关不掉
                set_topmost(self.bg, False)
                set_topmost(self.root, False)
                set_topmost(self.input, False)
                insert_below(self.bg, self.root)
                insert_below(self.root, self.input)
            if self._card is not None and getattr(self._card, "visible", False):
                force_topmost(self._card.win)
            if self._menu is not None:
                force_topmost(self._menu)
            for dlg in list(self._dialogs.values()):
                win = getattr(dlg, "win", None)
                if win is not None:
                    force_topmost(win)
        except tk.TclError:
            pass

    def _render_bar(self, force: bool = False):
        if not self.root.winfo_exists():
            return
        parts, pad, _ = self._bar_parts()
        line_h = max(p["font"].metrics("linespace") for p in parts) if parts else 16
        if self.cfg["appearance"] == "pill":
            h = int(line_h + 13 * self.scale)
        else:
            h = int(line_h + 10 * self.scale)
        radius = h / 2
        width = pad * 2
        for p in parts:
            width += p["gap"] + (1 if p["kind"] == "sep" else p["font"].measure(p["text"]))
        width = max(int(round(width)), int(40 * self.scale))
        sig = (self.cfg["appearance"], self.cfg["show_name"], self.cfg["theme"],
               self.cfg["font_size"], round(self.cfg["opacity"], 2),
               round(float(self.cfg.get("bg_opacity", 1.0)), 2),
               bool(self.cfg.get("dock_taskbar")),
               self._hovering, self._pressed, self._error,
               tuple((p["text"], p["color"], round(p["gap"], 1)) for p in parts))
        if not force and sig == getattr(self, "_bar_sig", None) and (width, h) == (self._w, self._h):
            return
        self._bar_sig = sig
        pal = self.pal
        self.canvas.delete("all")
        self.bg_canvas.delete("all")
        # 嵌入任务栏时不画胶囊底色: 文字直接落在任务栏上, 像天气组件一样"长"在任务栏里
        bg_op = self._effective_bg_op()
        text_op = float(self.cfg.get("opacity", 1.0))
        restack = False
        try:
            # 背景层 / 文字层各自独立透明度, 不再共用一个窗口 alpha
            # (属性只在变化时设置: Tk 改窗口属性会打乱 z 序)
            bg_a = max(0.0, min(1.0, bg_op))
            if bg_a != self._bg_alpha_on:
                self._bg_alpha_on = bg_a
                self.bg.attributes("-alpha", bg_a)
                restack = True
            tx_a = max(0.0, min(1.0, text_op))
            if tx_a != self._text_alpha_on:
                self._text_alpha_on = tx_a
                self.root.attributes("-alpha", tx_a)
                restack = True
        except tk.TclError:
            pass
        if self._apply_text_key():
            restack = True
        # 属性变化落定后再改几何: Tk 的 geometry() 是异步的,
        # 属性触发的窗口刷新会把未落地的移动请求冲掉
        restack = self._resize_bar(width, h) or restack
        if bg_op > 0.001:
            fill = pal["press"] if self._pressed else pal["bar_bg"]
            if self._error:
                border = pal["warn"]
            elif self._hovering:
                border = mix(pal["bar_border"], pal["accent"], 0.35)
            else:
                border = pal["bar_border"]
            draw_round_rect(self.bg_canvas, 0.5, 0.5, width - 0.5, h - 0.5,
                            radius, fill, border, 1)
        # 文字始终画在 root 层, 由 root 的 -alpha 控制文字不透明度
        c = self.canvas
        x = pad
        cy = h / 2
        self._index_zone_left = None
        self._zone_pnl_left = None
        for p in parts:
            x += p["gap"]
            color = p["color"]
            if p["kind"] == "sep":
                if self._zone_pnl_left is None:
                    self._zone_pnl_left = x     # 第一条分隔线 = 持仓段左界
                c.create_line(x, h * 0.3, x, h * 0.7, fill=color)
                x += 1
            else:
                if p["kind"] in ("idxname", "index") and self._index_zone_left is None:
                    self._index_zone_left = x - 4 * self.scale
                c.create_text(x, cy - 0.5, text=p["text"], anchor="w", fill=color,
                              font=p["font"])
                x += p["font"].measure(p["text"])
        if self._index_zone_left is None:
            self._index_zone_left = width
        if restack:
            self._restack_bar()

    def _resize_bar(self, w: int, h: int) -> bool:
        w, h = max(1, int(w)), max(1, int(h))
        if (w, h) == (self._w, self._h):
            return False
        if self._first_place:
            pos = self.cfg.get("window_pos")
            if isinstance(pos, dict):
                x, y = int(pos["x"]), int(pos["y"])
            else:
                wx, wy, ww, wh = self.screen.work_area(self.screen.vx + 100, self.screen.vy + 100)
                x = wx + ww - w - int(18 * self.scale)
                y = wy + wh - h - int(10 * self.scale)
            self._first_place = False
        else:
            x, y = self._x, self._y
        if self.cfg.get("dock_taskbar"):
            x, y = self._dock_pos(w, h)      # 嵌入任务栏(左): 位置由任务栏决定
        x, y = self.screen.clamp(x, y, w, h)
        self._x, self._y = x, y
        self._w, self._h = w, h
        self._wall_cache = None
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.canvas.place(x=0, y=0, width=w, height=h)
        self.bg.geometry(f"{w}x{h}+{x}+{y}")
        self.bg_canvas.place(x=0, y=0, width=w, height=h)
        self.input.geometry(f"{w}x{h}+{x}+{y}")
        self.root.update_idletasks()   # 先落地, 避免被后续层级维护冲掉
        return True

    def _set_pos(self, x: int, y: int):
        x, y = self.screen.clamp(x, y, self._w, self._h)
        self._x, self._y = x, y
        self.root.geometry(f"+{x}+{y}")
        self.bg.geometry(f"+{x}+{y}")
        self.input.geometry(f"+{x}+{y}")
        # Tk 的 geometry() 异步生效: 若随后有 SetWindowPos(层级维护)会把移动冲掉,
        # 这里立刻落位
        self.root.update_idletasks()
        self._wall_cache = None   # 位置变了, 重新采样壁纸

    def _startup_pulse(self):
        if not self.root.winfo_exists():
            return
        self._flash["pick"] = time.time() + 1.2
        self._render_bar(force=True)
        self.root.after(1300, lambda: self._render_bar(force=True))

    # ---------- 交互 ----------
    def _on_press(self, event):
        self._hide_card()
        self._pressed = True
        self._render_bar(force=True)
        self._drag = (event.x_root, event.y_root, self._x, self._y)
        try:
            self.input.focus_force()
        except tk.TclError:
            pass
        focus_window(self.input)   # OS 层焦点, 保证快捷键可用

    def _on_drag(self, event):
        if not self._drag:
            return
        dx = event.x_root - self._drag[0]
        dy = event.y_root - self._drag[1]
        if self.cfg.get("dock_taskbar"):
            # 嵌入模式: 只在任务栏条带内左右滑动
            bx, by, bw, bh = self.screen.taskbar_band(event.x_root, event.y_root)
            nx = max(bx, min(self._drag[2] + dx, bx + bw - self._w))
            self._set_pos(nx, by + max(0, (bh - self._h) // 2))
            return
        self._set_pos(self._drag[2] + dx, self._drag[3] + dy)

    def _on_release(self, event):
        if not self._drag:
            return
        dx = event.x_root - self._drag[0]
        dy = event.y_root - self._drag[1]
        self._drag = None
        was_pressed = self._pressed
        self._pressed = False
        self._render_bar(force=was_pressed)
        if abs(dx) + abs(dy) >= 4:
            self.cfg["window_pos"] = {"x": self._x, "y": self._y}
            self.save_config()
            self._restack_bar()
        elif event.x_root - self._x < (self._index_zone_left or self._w):
            self._switch(1)

    def _on_menu(self, event):
        self._hide_card()
        self._show_menu(event.x_root, event.y_root)

    def _switch(self, delta: int):
        if not self.pick_codes:
            return
        self.pick_index = (self.pick_index + delta) % len(self.pick_codes)
        self._hide_card()
        self._flash["pick"] = time.time() + 0.28
        self._render_bar(force=True)
        self.root.after(320, lambda: self._render_bar(force=True))

    def _select_pick(self, code: str):
        if code in self.pick_codes:
            self.pick_index = self.pick_codes.index(code)
            self._flash["pick"] = time.time() + 0.28
            self._hide_card()
            self._render_bar(force=True)

    # ---------- 悬停 ----------
    def _poll_mouse(self):
        try:
            if not self.root.winfo_exists():
                return
            px, py = self.root.winfo_pointerxy()
            x, y, w, h = self.bar_rect()
            inside = x <= px <= x + w and y <= py <= y + h
            if inside:
                rel = px - x
                if rel >= (self._index_zone_left or w):
                    zone = "index"
                elif self._zone_pnl_left is not None and rel >= self._zone_pnl_left:
                    zone = "pnl"
                else:
                    zone = "pick"
                if not self._hovering:
                    self._hovering = True
                    self._hover_zone = zone
                    self._render_bar(force=True)
                    self._schedule_card()
                elif zone != self._hover_zone:
                    self._hover_zone = zone
                    if self._card and self._card.visible:
                        self._show_card()
            else:
                if self._hovering:
                    self._hovering = False
                    self._cancel_hover()
                    self._render_bar(force=True)
                if self._card and self._card.visible:
                    self._card.hide()
            self._after_mouse = self.root.after(100, self._poll_mouse)
        except tk.TclError:
            pass

    def _schedule_card(self):
        if self._hover_after:
            try:
                self.root.after_cancel(self._hover_after)
            except tk.TclError:
                pass
        self._hover_after = self.root.after(260, self._show_card)

    def _cancel_hover(self):
        if self._hover_after:
            try:
                self.root.after_cancel(self._hover_after)
            except tk.TclError:
                pass
            self._hover_after = None

    def _show_card(self):
        self._hover_after = None
        if not self._hovering or self._menu:
            return
        if self._hover_zone == "pnl":
            self._card.show_holdings()      # 持仓段: 真实持仓总览
            return
        code = self.index_code if self._hover_zone == "index" else self._current_pick_code()
        if not code or code not in self.quotes:
            return
        self._card.show(code)

    def _hide_card(self):
        if self._card:
            self._card.hide()

    # ---------- 数据 ----------
    def _restart_worker(self, force: bool = False):
        codes = [c for c in self.pick_codes if c]
        for c in (self.cfg.get("holdings") or {}):
            if c and c not in codes:
                codes.append(c)   # 持仓但不在自选的也要拉行情
        if self.index_code and self.index_code not in codes:
            codes.append(self.index_code)
        interval = max(0.5, float(self.cfg["interval_seconds"]))
        spec = (tuple(codes), self.index_code, interval)
        if (not force and spec == getattr(self, "_worker_spec", None)
                and self._worker and self._worker.is_alive()):
            return
        self._worker_spec = spec
        self._gen += 1
        gen = self._gen
        old_stop = getattr(self, "_stop", None)
        if old_stop is not None:
            old_stop.set()
        self._stop = threading.Event()
        stop = self._stop
        self._worker = threading.Thread(target=self._worker_loop,
                                        args=(gen, codes, self.index_code, interval, stop),
                                        daemon=True)
        self._worker.start()

    def _worker_loop(self, gen: int, codes: list[str], index_code: str,
                     interval: float, stop: threading.Event):
        failures = 0
        while not stop.is_set():
            started = time.time()
            try:
                quotes = D.fetch_quotes(codes, index_codes=[index_code], timeout=6.0)
                if quotes:
                    self._q.put(("data", gen, quotes))
                    failures = 0
                else:
                    failures += 1
                    self._q.put(("error", gen, "empty"))
            except Exception as exc:
                failures += 1
                self._q.put(("error", gen, str(exc)))
            wait = interval if failures == 0 else min(30.0, interval * (2 ** min(failures, 4)))
            elapsed = time.time() - started
            stop.wait(max(0.1, wait - elapsed))

    def _poll_queue(self):
        if not self.root.winfo_exists():
            return
        # 看门狗: 取数线程挂死(DNS/网络抖动)超过 20 秒没有消息就自动重启
        if self._worker is not None and time.time() - self._last_msg_at > 20.0:
            self._last_msg_at = time.time()
            self._log("行情线程超时无响应, 自动重启")
            self._restart_worker(force=True)
        try:
            while True:
                kind, *payload = self._q.get_nowait()
                self._last_msg_at = time.time()
                try:
                    if kind == "data":
                        gen, quotes = payload
                        if gen == self._gen:
                            self._on_data(quotes)
                    elif kind == "error":
                        gen = payload[0]
                        if gen == self._gen:
                            self._set_error(True)
                    elif kind == "minutes":
                        code, series = payload
                        self._on_minutes(code, series)
                except Exception as exc:
                    # 单条消息处理失败(如某次渲染异常)不能拖垮整个轮询链
                    self._log("处理 %s 消息异常: %r" % (kind, exc))
        except queue.Empty:
            pass
        except Exception as exc:
            self._log("轮询异常: %r" % exc)
        self._tick += 1
        # 嵌入任务栏时任务栏会把自己抬到最上层, 需要更频繁地压回去
        every = 3 if self.cfg.get("dock_taskbar") else 20
        if self._tick % every == 0:
            self._keep_on_top()
        try:
            self._after_queue = self.root.after(100, self._poll_queue)
        except tk.TclError:
            pass

    def _on_data(self, quotes: dict):
        self.quotes.update(quotes)
        if self._error:
            self._error = False
        now = time.time()
        for role, code in (("pick", self._current_pick_code()), ("index", self.index_code)):
            new = quotes.get(code)
            if new is None:
                continue
            old = self._last_vals.get(role)
            if old is not None and abs(old - new.pct) > 1e-9:
                self._flash[role] = now + 0.22
            self._last_vals[role] = new.pct
        pnl = self._pnl_display_value()
        if pnl is not None and self._last_vals.get("pnl") is not None:
            if abs(self._last_vals["pnl"] - pnl) > 1e-9:
                self._flash["pnl"] = now + 0.22
        self._last_vals["pnl"] = pnl
        self._render_bar(force=True)
        if self._card and self._card.visible:
            self._card.refresh()
        if any(v > now for v in self._flash.values()):
            self.root.after(240, self._safe_render)

    def _safe_render(self):
        """延迟渲染的安全包装: 窗口销毁后残留的 after 回调不再报错。"""
        try:
            self._render_bar(force=True)
        except tk.TclError:
            pass

    def _tk_callback_error(self, exc, val, tb):
        """Tk 回调异常统一进日志(打包成窗口程序时写 stderr 可能引发二次崩溃)。"""
        try:
            self._log("界面回调异常: %s: %s" % (getattr(exc, "__name__", exc), val))
        except Exception:
            pass

    def _set_error(self, error: bool):
        if self._error != error:
            self._error = error
            self._render_bar(force=True)
            if self._card and self._card.visible:
                self._card.refresh()

    def _total_pnl_pct(self):
        total_pnl = 0.0
        total_base = 0.0
        holdings = self.cfg.get("holdings", {})
        codes = [c for c in self.pick_codes if c]
        for c in holdings:
            if c and c not in codes:
                codes.append(c)
        for code in codes:
            h = holdings.get(code)
            q = self.quotes.get(code)
            if not h or not q:
                continue
            pos, _ = D.position_of(h.get("trades"))
            if pos.lots <= 0:
                continue
            shares = pos.lots * 100
            total_pnl += (q.price - q.prev_close) * shares
            total_base += q.prev_close * shares
        if not total_base:
            return None
        return total_pnl / total_base * 100.0

    def _total_day_pnl(self):
        """持仓当日盈亏合计(元): 老仓按昨收、当日新买按买入价(见 D.day_pnl_of)。"""
        total = 0.0
        holdings = self.cfg.get("holdings", {})
        codes = [c for c in self.pick_codes if c]
        for c in holdings:
            if c and c not in codes:
                codes.append(c)
        for code in codes:
            h = holdings.get(code)
            q = self.quotes.get(code)
            if not h or not q:
                continue
            pos, _ = D.position_of(h.get("trades"))
            if pos.lots <= 0 and not D.sold_today(h.get("trades")):
                continue                 # 今日清仓也计入(今天亏的钱就是今天的)
            total += D.day_pnl_of(h.get("trades"), q.price, q.prev_close)
        return total

    def _pnl_display_value(self):
        """持仓段显示值: 按设置返回涨幅(%)或当日盈亏(元)。"""
        if self.cfg.get("pnl_display", "money") == "pct":
            return self._total_pnl_pct()
        return self._total_day_pnl()

    # ---------- 分时 ----------
    def request_minutes(self, code: str, callback):
        if not code:
            return
        cached = self._minute_cache.get(code)
        if cached:
            callback(code, cached[1])
            if time.time() - cached[0] < 30:
                return
        self._minute_waiters.setdefault(code, []).append(callback)
        if code in self._minute_pending:
            return
        self._minute_pending.add(code)
        threading.Thread(target=self._minute_worker, args=(code,), daemon=True).start()

    def _minute_worker(self, code: str):
        series = None
        try:
            series = D.fetch_minutes(code, timeout=4.0)
        except Exception:
            series = None
        self._q.put(("minutes", code, series))

    def _on_minutes(self, code: str, series):
        self._minute_cache[code] = (time.time(), series)
        self._minute_pending.discard(code)
        for cb in self._minute_waiters.pop(code, []):
            try:
                cb(code, series)
            except Exception:
                pass

    # ---------- 菜单 ----------
    def _show_menu(self, x: int, y: int):
        pal, F = self.pal, self.fonts
        pick_code = self._current_pick_code()
        items = [
            {"type": "cmd", "label": "设置…", "shortcut": "Ctrl+,", "command": self.open_settings},
            {"type": "sep"},
            {"type": "cmd", "label": "管理自选股…", "shortcut": "Ctrl+M", "command": self.open_manage},
            {"type": "cmd", "label": "持仓设置…", "shortcut": "Ctrl+H", "command": self.open_holdings},
        ]
        stock_items = []
        for code in self.pick_codes:
            q = self.quotes.get(code)
            label = _short_name(q.name, 6) if q else D.code_digits(code)
            if q:
                label += "  " + D.fmt_pct(q.pct)
            stock_items.append({"type": "check", "label": label, "checked": code == pick_code,
                                "checked_fn": (lambda c=code: c == self._current_pick_code()),
                                "command": lambda c=code: self._select_pick(c)})
        if stock_items:
            items.append({"type": "sub", "label": "切换股票", "items": stock_items})
        iv_items = []
        for sec in INTERVALS:
            iv_items.append({"type": "check", "label": f"{sec:g} 秒",
                             "checked": abs(self.cfg["interval_seconds"] - sec) < 1e-6,
                             "checked_fn": (lambda s=sec: abs(self.cfg["interval_seconds"] - s) < 1e-6),
                             "command": lambda s=sec: self.set_interval(s)})
        items.append({"type": "sub", "label": "刷新间隔", "items": iv_items})
        items.append({"type": "check", "label": "窗口置顶", "checked": bool(self.cfg["topmost"]),
                      "checked_fn": lambda: bool(self.cfg["topmost"]),
                      "command": self.toggle_topmost})
        items.append({"type": "check", "label": "嵌入任务栏（左）",
                      "checked": bool(self.cfg.get("dock_taskbar")),
                      "checked_fn": lambda: bool(self.cfg.get("dock_taskbar")),
                      "command": self.toggle_dock})
        items.append({"type": "sep"})
        items.append({"type": "cmd", "label": "复制当前行情", "shortcut": "Ctrl+C",
                      "command": self.copy_quotes})
        items.append({"type": "cmd", "label": "回到右下角", "command": self.move_to_corner})
        items.append({"type": "cmd", "label": "重新加载配置", "shortcut": "F5",
                      "command": self.reload_config})
        items.append({"type": "cmd", "label": "编辑配置文件", "command": self.edit_config})
        items.append({"type": "sep"})
        items.append({"type": "cmd", "label": "退出", "command": self.quit, "danger": True})
        menu = MenuPopup(self.root, items, pal, F, scale=self.scale,
                         on_close=self._menu_closed)
        menu.show_at(x, y)
        self._menu = menu

    def _menu_closed(self):
        self._menu = None
        # 菜单会抢走焦点, 关闭后还给交互层, 快捷键继续可用
        try:
            self.input.focus_force()
        except tk.TclError:
            pass
        focus_window(self.input)

    # ---------- 设置 ----------
    def save_config(self) -> bool:
        return save_config(self.cfg)

    def apply_settings(self):
        self.cfg = normalize_config(self.cfg)
        self.save_config()
        self.pal = palette(self.cfg["theme"], self.cfg["cn_colors"])
        self.fonts = Fonts(self.root, self.cfg["font_size"])
        # 窗口属性只在真正变化时设置: 无谓的属性调用会打乱 z 序并触发整窗重绘
        restack = False
        try:
            topmost = bool(self.cfg["topmost"])
            if topmost != self._topmost_on:
                self._topmost_on = topmost
                self.root.attributes("-topmost", topmost)
                self.bg.attributes("-topmost", topmost)
                self.input.attributes("-topmost", topmost)
                restack = True
        except tk.TclError:
            pass
        self._load_lists()
        self._bar_sig = None
        # 透明度/文字色键由 _render_bar 按需设置, 并在必要时恢复层级
        self._render_bar(force=True)
        self._restart_worker()
        if restack:
            self._restack_bar()
        if self._card and self._card.visible:
            self._card.refresh()

    def set_interval(self, sec: float):
        self.cfg["interval_seconds"] = float(sec)
        self.save_config()
        self._restart_worker()

    def toggle_topmost(self):
        self.cfg["topmost"] = not bool(self.cfg["topmost"])
        self.apply_settings()

    def set_stocks(self, codes: list[str]):
        self.cfg["stocks"] = codes
        self.save_config()
        self.apply_settings()

    def refresh_pnl(self):
        self._render_bar(force=True)
        if self._card and self._card.visible:
            self._card.refresh()

    def reset_appearance(self):
        keep = {k: self.cfg.get(k) for k in ("stocks", "holdings", "window_pos")}
        cfg = _default_config()
        cfg.update({k: v for k, v in keep.items() if v is not None})
        self.cfg = normalize_config(cfg)
        self.save_config()
        self.apply_settings()

    def open_settings(self):
        self._open_dialog("settings", lambda: sb_ui.SettingsWindow(self))

    def open_manage(self):
        self._open_dialog("manage", lambda: sb_ui.ManageStocksWindow(self))

    def open_holdings(self):
        self._open_dialog("holdings", lambda: sb_ui.HoldingsWindow(self))

    def _open_dialog(self, key: str, factory):
        win = self._dialogs.get(key)
        if win is not None:
            try:
                win.raise_()
                return
            except Exception:
                self._dialogs.pop(key, None)
        self._hide_card()
        try:
            factory()
        except Exception:
            import traceback
            self._log("打开窗口失败:\n" + traceback.format_exc())

    def is_autostart(self) -> bool:
        return is_autostart()

    def set_autostart(self, enabled: bool) -> bool:
        ok = set_autostart(enabled)
        if not ok:
            self._log("写入开机自启失败")
        return ok

    def reload_config(self):
        self.cfg = load_config()
        self.apply_settings()
        self._log("配置已重新加载")

    def edit_config(self):
        if not os.path.exists(CONFIG_PATH):
            self.save_config()
        try:
            os.startfile(CONFIG_PATH)
        except OSError:
            pass

    # ---------- 任务栏嵌入 ----------
    def _dock_pos(self, w: int | None = None, h: int | None = None) -> tuple[int, int]:
        """嵌入任务栏(左)时的目标坐标: 优先落在任务栏空位上, 垂直居中。"""
        w = self._w if w is None else w
        h = self._h if h is None else h
        bx, by, bw, bh = self.screen.taskbar_band(self._x + self._w // 2,
                                                 self._y + self._h // 2)
        # 找任务栏里的空位(避开天气组件/开始按钮等已有元素), 找不到才贴左端
        gap = self.screen.find_free_span(bx, by, bw, bh, w + int(24 * self.scale),
                                        ignore=(self._x, self._x + self._w))
        x = gap if gap is not None else bx + int(12 * self.scale)
        y = by + max(0, (bh - h) // 2)
        return self.screen.clamp(x, y, w, h)

    def set_dock(self, enabled: bool):
        self.cfg["dock_taskbar"] = bool(enabled)
        # 先渲染(把 alpha/色键等属性变化落定), 再移动:
        # Tk 的 geometry() 是异步的, 属性变化会把未落地的移动请求弄丢
        self._render_bar(force=True)
        if enabled:
            self._set_pos(*self._dock_pos())
            self._dock_band = self.screen.taskbar_band(self._x + self._w // 2,
                                                       self._y + self._h // 2)
            self.cfg["window_pos"] = {"x": self._x, "y": self._y}
        self.save_config()
        self._restack_bar()

    def toggle_dock(self):
        self.set_dock(not bool(self.cfg.get("dock_taskbar")))

    def move_to_corner(self):
        self.cfg["dock_taskbar"] = False
        wx, wy, ww, wh = self.screen.work_area(self.screen.vx + 100, self.screen.vy + 100)
        x = wx + ww - self._w - int(18 * self.scale)
        y = wy + wh - self._h - int(10 * self.scale)
        self._set_pos(x, y)
        self.cfg["window_pos"] = {"x": self._x, "y": self._y}
        self.save_config()

    # ---------- 复制 ----------
    def copy_quotes(self):
        lines = [f"{APP_NAME}  {time.strftime('%Y-%m-%d %H:%M:%S')}"]
        for code in [self._current_pick_code(), self.index_code]:
            q = self.quotes.get(code)
            if not q:
                continue
            lines.append(f"{q.name}({D.code_digits(code)}) {D.fmt_price(q.price, q.dp)} "
                         f"{D.fmt_change(q.change, q.dp)} {D.fmt_pct(q.pct)}")
            h = self.cfg.get("holdings", {}).get(code)
            if h and not q.is_index:
                pos, _ = D.position_of(h.get("trades"))
                if pos.lots > 0:
                    dpnl = D.day_pnl_of(h.get("trades"), q.price, q.prev_close)
                    lines.append(f"    持仓 {pos.lots}手  当日盈亏 {D.fmt_money_signed(dpnl)}")
        try:
            self.input.clipboard_clear()
            self.input.clipboard_append("\n".join(lines))
            self._toast("已复制行情")
        except tk.TclError:
            pass

    def _toast(self, _text: str):
        """轻量反馈: 让边框闪一下。"""
        self._flash["pick"] = time.time() + 0.25
        self._render_bar(force=True)

    # ---------- 置顶 ----------
    def _keep_on_top(self):
        self._restack_bar()
        if self.cfg.get("dock_taskbar"):
            # 任务栏位置/高度变化(换显示器、改设置等)时重新嵌入
            band = self.screen.taskbar_band(self._x + self._w // 2, self._y + self._h // 2)
            if band != getattr(self, "_dock_band", None):
                self._dock_band = band
                self._set_pos(*self._dock_pos())

    # ---------- 启动/退出 ----------
    def _cancel_pollers(self):
        for attr in ("_after_queue", "_after_mouse"):
            aid = getattr(self, attr, None)
            if aid:
                try:
                    self.root.after_cancel(aid)
                except tk.TclError:
                    pass
                setattr(self, attr, None)

    def run(self):
        self._log(f"启动 v{APP_VERSION}, 自选 {len(self.pick_codes)} 只, 模式 {self.cfg['appearance']}")
        try:
            self.root.mainloop()
        finally:
            self._stop.set()
            self._cancel_pollers()
            try:
                self.root.destroy()
            except tk.TclError:
                pass
            self._log("退出")
            _remove_lock()

    def quit(self):
        self._log("用户退出")
        self._stop.set()
        self._cancel_pollers()
        try:
            self.root.destroy()
        except tk.TclError:
            pass


# ---------------- 自检 ----------------
def _disp_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _fit(text: str, width: int) -> str:
    """按显示宽度截断(含中文), 再补齐到 width。"""
    text = str(text or "")
    out, used = "", 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + cw > width - 1:
            out += "…"
            used += 1
            break
        out += ch
        used += cw
    return out + " " * max(0, width - used)


def check_mode() -> int:
    cfg = load_config()
    codes = [c for c in cfg["stocks"]]
    if cfg["index"] not in codes:
        codes.append(cfg["index"])
    print(f"自选 {len(cfg['stocks'])} 只 + 指数 {cfg['index']}，正在拉取实时行情…\n")
    try:
        quotes = D.fetch_quotes(codes, index_codes=[cfg["index"]])
    except Exception as exc:
        print("拉取失败:", exc)
        return 1
    head = (_fit("代码", 10) + _fit("名称", 16) + _fit("现价", 10) +
            _fit("涨跌", 9) + _fit("涨跌幅", 9) + "行情时间")
    print(head)
    for code in codes:
        q = quotes.get(code)
        if q:
            tag = " [指数]" if code == cfg["index"] else ""
            print(_fit(code, 10) + _fit(q.name, 16) + _fit(D.fmt_price(q.price, q.dp), 10) +
                  _fit(D.fmt_change(q.change, q.dp), 9) + _fit(D.fmt_pct(q.pct), 9) +
                  D.fmt_quote_time(q.time) + tag)
        else:
            print(_fit(code, 10) + "无数据")
    print(f"\n共获取 {len(quotes)} 条。代码格式: sh600519 / sz000001 / hk00700 / usAAPL。")
    return 0


def main() -> int:
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0
    if "--version" in sys.argv or "-v" in sys.argv:
        print(f"{APP_NAME} v{APP_VERSION}")
        return 0
    if "--autostart" in sys.argv:
        enable = "--on" in sys.argv
        ok = set_autostart(enable)
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") +
                        f"autostart test enable={enable} ok={ok} current={is_autostart()}\n")
        except OSError:
            pass
        return 0 if ok else 1
    if "--check" in sys.argv:
        return check_mode()

    enable_dpi_awareness()
    pid = _instance_pid()
    if pid:
        _flash_existing()
        return 0
    _write_lock()
    try:
        cfg = load_config()
        save_config(cfg)
        app = StockBar(cfg)
        app.run()
        return 0
    except Exception:
        import traceback
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + "启动失败:\n")
                traceback.print_exc(file=f)
        except OSError:
            pass
        raise
    finally:
        _remove_lock()


if __name__ == "__main__":
    sys.exit(main())
