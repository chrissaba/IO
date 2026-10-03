"""IO regression benchmark: runs the tasks in bench/tasks.json against IO and checks every answer and side effect by
machine, so "it got better here but worse somewhere else" shows up as a number (spec section 8).

Drivers:
  direct (default)  boss.run() in this process, with the code on disk now: tests a change without restarting IO
  http              the running app through its API (after the user restarted IO)

    .venv\\Scripts\\python.exe bench\\run_bench.py --only chat,P1 --director off
    .venv\\Scripts\\python.exe bench\\run_bench.py --suite smoke --director off,on --repeat 2 --save-baseline
    .venv\\Scripts\\python.exe bench\\run_bench.py --suite full --mode balanced --baseline bench\\baseline.json

It shares the PC with IO and with you: it waits until IO is idle, pauses IO's queue while it runs (direct driver),
skips tasks whose app you already have open, closes only windows created during a task, discards Notepad tabs only
when their text is the bench's own, and restores your clipboard, IO's paused state and IO's model mode at the end.
The bench itself never imports actions.py; the direct driver's boss.run does, so it measures the code on disk. To measure
without the action layer, set data/actions.json to {"enabled": false} (note: the running IO reads that file too, within 5 s).
"""
import argparse
import asyncio
import atexit
import contextlib
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import shutil
import socket
import statistics
import sys
import time
import traceback
import urllib.error
import urllib.request
import winreg
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
API = os.environ.get("IO_API", "http://127.0.0.1:8765")
SANDBOX = Path(os.environ.get("TEMP") or os.environ.get("TMP") or str(Path.home())) / "io-bench"
TASKS_FILE = HERE / "tasks.json"
BASELINE_FILE = HERE / "baseline.json"

# physical pixels, like boss.py: pointer positions and window rects must agree with what IO sees
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
user32 = ctypes.windll.user32

# the apps tasks name in requires/setup/teardown/window checks: how to recognise their windows
APPS = {
    "notepad": {"proc": r"^notepad\.exe$", "title": r"Notepad$"},
    "calculator": {"title": r"^Calculator$"},
    "settings": {"title": r"^Settings$", "proc": r"^(ApplicationFrameHost|SystemSettings)\.exe$"},
    "explorer": {"proc": r"^explorer\.exe$", "cls": r"^CabinetWClass$"},
    "bluestacks": {"title": r"BlueStacks"},
}
OPENERS = {"notepad": ["notepad.exe"], "calculator": ["calc.exe"]}
CHECK_TYPES = {
    "answer_nonempty", "answer_regex", "no_answer_regex", "answer_gt", "answer_gt_bool", "answer_time_near",
    "answer_number_near", "answer_mentions_any", "tool_used", "no_tool", "max_tools", "tool_count", "event_present",
    "max_director_rounds", "director_parse_ok", "director_parse_rate", "max_secs", "asked", "status", "window",
    "no_new_window", "file", "no_process", "tools_chars_max",
}
POLICY_CHECKS = {"max_tools", "max_director_rounds", "tools_chars_max"}
SETUP_OPS = {"sandbox", "write", "files", "bigfiles", "open", "maximize", "clip"}
# windows that come and go on their own: never a leftover, never "your window disappeared"
IGNORE_TITLES = re.compile(r"^(Program Manager|Windows Input Experience|NVIDIA GeForce Overlay|Task Switching|Start|Search|"
                           r"Notification Center|Default IME|MSCTFIME UI)$")
# tabs IO must not leave behind in Chrome (its Duck.ai chat, the sentinel page, the extension's connect page)
STALE_TABS = r"Duck\.ai|IO is done|IO is connected|about:blank"
RISKY_PS = re.compile(r"\b(Stop-Computer|Restart-Computer|shutdown|Format-Volume|format|Set-ExecutionPolicy|reg\s+delete|"
                      r"Uninstall-\w+|Send-MailMessage|Stop-Process|taskkill|kill)\b", re.I)


def say(*parts) -> None:
    """The bench's own output: always to the real console, even while boss.py's log lines go to a file. Window titles
    with emoji can't be printed on a cp1252 console, and a closed pipe mustn't stop the run."""
    with contextlib.suppress(OSError, ValueError):
        if sys.__stdout__.errors == "strict":
            sys.__stdout__.reconfigure(errors="replace")
        print(*parts, file=sys.__stdout__, flush=True)


# ---------- IO's API ----------

def api(path: str, body: dict | None = None, method: str = "", timeout: float = 10) -> dict:
    req = urllib.request.Request(API + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method or ("POST" if body is not None else "GET"))
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def io_state() -> dict | None:
    try:
        return api("/api/state")
    except Exception:
        return None


def io_busy(state: dict) -> list[str]:
    return [f"{t['id']} {t['status']}: {t['text'][:50]}" for t in state.get("tasks", []) if t.get("status") in ("queued", "running", "waiting")]


class Restore:
    """Undo steps that must run however the bench ends (finally, Ctrl+C or interpreter exit), each only once."""

    def __init__(self):
        self.steps: list = []
        atexit.register(self.run)

    def add(self, name: str, fn) -> None:
        self.steps.append((name, fn))

    def run(self) -> None:
        while self.steps:
            name, fn = self.steps.pop()
            try:
                fn()
            except Exception as e:
                say(f"  couldn't restore {name}: {e}")


RESTORE = Restore()


# ---------- desktop: windows, clipboard, pointer, processes ----------

def cloaked(hwnd: int) -> bool:
    """Suspended UWP frames (a closed Settings window) and other-desktop windows are 'visible' but not on screen."""
    v = ctypes.c_int(0)
    return ctypes.windll.dwmapi.DwmGetWindowAttribute(wt.HWND(hwnd), 14, ctypes.byref(v), 4) == 0 and v.value != 0


def windows() -> list[dict]:
    """Top-level app windows on screen (minimized ones too): hwnd, title, class, pid, process name, zoomed."""
    out: list[dict] = []
    names: dict[int, str] = {}

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4) and not cloaked(hwnd):  # GW_OWNER: no popups
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                cls = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls, 256)
                pid = wt.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value not in names:
                    try:
                        names[pid.value] = psutil.Process(pid.value).name()
                    except Exception:
                        names[pid.value] = "?"
                r = wt.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                tiny = r.right - r.left < 40 or r.bottom - r.top < 40  # helper windows (Unsloth's 15x15 one)
                if not IGNORE_TITLES.match(buf.value) and (not tiny or user32.IsIconic(hwnd)):
                    out.append({"hwnd": int(hwnd), "title": buf.value, "cls": cls.value, "pid": pid.value, "proc": names[pid.value],
                                "zoomed": bool(user32.IsZoomed(hwnd)), "iconic": bool(user32.IsIconic(hwnd))})
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)(cb), 0)
    return out


def app_match(w: dict, app: str | None = None, title: str | None = None) -> bool:
    spec = APPS.get(app or "", {})
    for key, field in (("proc", "proc"), ("title", "title"), ("cls", "cls")):
        if key in spec and not re.search(spec[key], w[field], re.I):
            return False
    return not title or bool(re.search(title, w["title"], re.I))


def _uia():
    from windows_mcp import uia  # COM UI Automation, initialised for this (main) thread on import
    return uia


def walk(hwnd: int, max_nodes: int = 4000, max_depth: int = 25, skip_documents: bool = False):
    """(control, depth) for one window's UIA tree, depth-first. Chrome's page content is skipped when asked: only its
    tab strip matters and a page can hold thousands of nodes."""
    uia = _uia()
    root = uia.ControlFromHandle(hwnd)
    if root is None:
        return
    stack, n = [(root, 0)], 0
    while stack and n < max_nodes:
        c, d = stack.pop()
        n += 1
        yield c, d
        if d >= max_depth or (skip_documents and c.ControlTypeName == "DocumentControl"):
            continue
        try:
            kids = c.GetChildren()
        except Exception:
            kids = []
        stack.extend((k, d + 1) for k in reversed(kids))


def window_text(hwnd: int) -> str:
    """Everything readable in a window through UIA (names, plus the full text of edit and document controls), the way
    read_window would see it. Read-only: no clipboard, no keys."""
    uia = _uia()
    parts = []
    try:
        for c, _ in walk(hwnd):
            if c.Name:
                parts.append(c.Name)
            if c.ControlTypeName in ("EditControl", "DocumentControl"):
                for pid, read in ((uia.PatternId.TextPattern, lambda p: p.DocumentRange.GetText(50000)),
                                  (uia.PatternId.ValuePattern, lambda p: p.Value)):
                    try:
                        p = c.GetPattern(pid)
                        if p:
                            parts.append(read(p) or "")
                            break
                    except Exception:
                        pass
    except Exception as e:
        parts.append(f"(couldn't read the window: {e})")
    return "\n".join(parts)


def chrome_tabs() -> list[str]:
    """Names of every Chrome tab (UIA TabItems of chrome.exe windows), read-only."""
    names = []
    for w in windows():
        if w["proc"].lower() == "chrome.exe":
            try:
                names += [c.Name for c, _ in walk(w["hwnd"], max_nodes=3000, max_depth=16, skip_documents=True)
                          if c.ControlTypeName == "TabItemControl"]
            except Exception:
                pass
    return names


def focus(hwnd: int) -> bool:
    """Brings a window to the front (Windows only lets the foreground app hand over focus: the Alt tap satisfies that)."""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
    user32.keybd_event(0x12, 0, 0, 0)
    user32.SetForegroundWindow(hwnd)
    user32.keybd_event(0x12, 0, 2, 0)
    time.sleep(0.3)
    return user32.GetForegroundWindow() == hwnd


def wm_close(hwnd: int, wait: float = 5) -> bool:
    user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE: like clicking X
    end = time.time() + wait
    while time.time() < end:
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd) or cloaked(hwnd):
            return True
        time.sleep(0.25)
    return False


def notepad_close(hwnd: int, discard: str) -> str:
    """Closes a Notepad window tab by tab. Win11 Notepad keeps unsaved tabs for its next session when its window is
    closed, so each tab is closed with Ctrl+W instead, and "Don't save" is pressed only for a tab that is unmodified,
    empty, or whose text matches `discard` (the bench's own text). Anything else is left open and reported."""
    for _ in range(15):
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
            return ""
        doc, tab, dont_save = None, None, None
        try:
            for c, _d in walk(hwnd, max_nodes=1500):
                ct = c.ControlTypeName
                if ct == "DocumentControl" and doc is None:
                    doc = c
                elif ct == "TabItemControl":
                    try:
                        if c.GetPattern(_uia().PatternId.SelectionItemPattern).IsSelected:
                            tab = c
                    except Exception:
                        tab = tab or c
                elif ct == "ButtonControl" and c.AutomationId == "SecondaryButton" and re.match(r"don.t save", c.Name or "", re.I):
                    dont_save = c
        except Exception as e:
            return f"couldn't read Notepad: {e}"
        text = ""
        if doc is not None:
            try:
                text = doc.GetPattern(_uia().PatternId.TextPattern).DocumentRange.GetText(50000) or ""
            except Exception:
                text = ""
        unmodified = bool(tab is not None and re.search(r"\bUnmodified\.?$", tab.Name or ""))
        if not (unmodified or not text.strip() or re.search(discard, text, re.I)):
            return f"left Notepad open: its tab holds text that isn't the bench's ({text[:60]!r})"
        if dont_save is not None:
            try:
                dont_save.GetPattern(_uia().PatternId.InvokePattern).Invoke()
            except Exception as e:
                return f"couldn't press Don't save: {e}"
            time.sleep(0.8)
            continue
        try:
            if doc is not None:
                doc.SetFocus()
        except Exception:
            pass
        if user32.GetForegroundWindow() != hwnd and not focus(hwnd):
            return "left Notepad open: couldn't bring it to the front to close its tab"
        if user32.GetForegroundWindow() != hwnd:  # never send Ctrl+W anywhere else (it closes a browser tab)
            return "left Notepad open: lost focus"
        user32.keybd_event(0x11, 0, 0, 0)
        user32.keybd_event(0x57, 0, 0, 0)
        user32.keybd_event(0x57, 0, 2, 0)
        user32.keybd_event(0x11, 0, 2, 0)
        time.sleep(0.8)
    return "left Notepad open: it kept asking"


CF_UNICODETEXT = 13


def clip_read() -> tuple[str | None, list[int]]:
    """(text or None, clipboard formats)."""
    import win32clipboard as cb
    for _ in range(10):
        try:
            cb.OpenClipboard()
        except Exception:
            time.sleep(0.1)
            continue
        try:
            formats, f = [], 0
            while True:
                f = cb.EnumClipboardFormats(f)
                if not f:
                    break
                formats.append(f)
            text = cb.GetClipboardData(CF_UNICODETEXT) if CF_UNICODETEXT in formats else None
            return text, formats
        finally:
            cb.CloseClipboard()
    raise RuntimeError("the clipboard stayed locked by another app")


def clip_write(text: str) -> None:
    import win32clipboard as cb
    for _ in range(10):
        try:
            cb.OpenClipboard()
        except Exception:
            time.sleep(0.1)
            continue
        try:
            cb.EmptyClipboard()
            cb.SetClipboardText(text, CF_UNICODETEXT)
            return
        finally:
            cb.CloseClipboard()
    raise RuntimeError("the clipboard stayed locked by another app")


def clip_clear() -> None:
    import win32clipboard as cb
    cb.OpenClipboard()
    try:
        cb.EmptyClipboard()
    finally:
        cb.CloseClipboard()


def pointer() -> tuple[int, int]:
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def procs(pattern: str, cmd: bool = False) -> dict[int, str]:
    """pid -> name (or command line) of running processes whose name (or command line) matches."""
    out = {}
    for p in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            text = " ".join(p.info["cmdline"] or []) if cmd else (p.info["name"] or "")
            if re.search(pattern, text, re.I):
                out[p.info["pid"]] = text[:200]
        except Exception:
            pass
    return out


def node_extension() -> set[int]:
    """IO's Playwright MCP servers that talk to the Chrome extension."""
    return {pid for pid, c in procs(r"cli\.js", cmd=True).items() if "--extension" in c and "node" in c.lower()}


def headless_edge() -> set[int]:
    return {pid for pid, c in procs(r"msedge", cmd=True).items() if "--headless" in c}


def primary_rect() -> tuple[int, int, int, int]:
    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]
    mon = user32.MonitorFromPoint(wt.POINT(0, 0), 1)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    user32.GetMonitorInfoW(mon, ctypes.byref(mi))
    r = mi.rcWork
    return r.left, r.top, r.right, r.bottom


# ---------- ground truth (computed at run time, never hard-coded) ----------

def expand(path: str) -> str:
    return os.path.expandvars(path.replace("%BENCH%", str(SANDBOX)))


def gt_tokyo_time(ctx, _arg=None):
    """Tokyo has no daylight saving time: UTC+9, in minutes of the day, for the task's start and end."""
    def mins(t):
        d = datetime.fromtimestamp(t, timezone.utc) + timedelta(hours=9)
        return d.hour * 60 + d.minute
    return mins(ctx["t0"]), mins(ctx.get("t1") or time.time())


def gt_c_free_bytes(_ctx, _arg=None):
    return shutil.disk_usage("C:\\").free


def _uninstall_names() -> list[str]:
    names = []
    for hive, path in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                       (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                       (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")):
        try:
            with winreg.OpenKey(hive, path) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sub:
                            names.append(str(winreg.QueryValueEx(sub, "DisplayName")[0]))
                    except OSError:
                        pass
        except OSError:
            pass
    return names


def gt_app_installed(_ctx, name: str) -> bool:
    """Uninstall entries, Start Menu shortcuts, %LOCALAPPDATA%\\Programs, Program Files folders and Steam manifests."""
    want = re.sub(r"\s+", "", name.lower())
    seen = list(_uninstall_names())
    for root in (Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
                 Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs"):
        try:
            seen += [p.stem for p in root.rglob("*.lnk")]
        except OSError:
            pass
    for root in (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs", Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
                 Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))):
        try:
            seen += [p.name for p in root.iterdir()]
        except OSError:
            pass
    seen += steam_names()
    return any(want in re.sub(r"\s+", "", s.lower()) for s in seen)


def gt_open_window_words(ctx, _arg=None) -> list[list[str]]:
    """Per window open when the task started (not IO's own), its title words of 4+ letters: one group per window."""
    skip = {"window", "windows", "microsoft", "official", "server", "untitled", "google", "page", "home", "with", "from", "this", "that"}
    groups = []
    for w in ctx["pre_windows"]:
        if w["title"] == "IO":
            continue
        words = list(dict.fromkeys(x for x in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", w["title"]) if x.lower() not in skip))
        if words:
            groups.append(words)
    return groups


def gt_cpu_logical_cores(_ctx, _arg=None) -> int:
    return os.cpu_count() or 0


def gt_cpu_cores(_ctx, _arg=None) -> list[int]:
    """Physical cores or logical processors: either is a right answer to "how many cores" (12C/24T says 12 or 24)."""
    return sorted({psutil.cpu_count(logical=False) or 0, os.cpu_count() or 0} - {0})


def gt_ipv4s(_ctx, _arg=None) -> list[str]:
    ips = set()
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254.")):
                ips.add(a.address)
    return sorted(ips)


def gt_steam_games(_ctx, _arg=None) -> list[list[str]]:
    """Installed Steam games, one group per game: its name, and the name without symbols like the trademark sign."""
    return [list(dict.fromkeys([n, re.sub(r"\s+", " ", re.sub(r"[^\w\s:'&-]", "", n)).strip()])) for n in steam_names()]


def steam_names() -> list[str]:
    roots = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            roots.append(Path(winreg.QueryValueEx(k, "SteamPath")[0]))
    except OSError:
        pass
    roots += [Path(r"C:\Program Files (x86)\Steam"), Path(r"C:\Program Files\Steam")]
    libs = set()
    for r in roots:
        vdf = r / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            libs.add(r)
            libs.update(Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text(encoding="utf-8", errors="replace")))
    games = set()
    for lib in libs:
        for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
            m = re.search(r'"name"\s+"([^"]+)"', acf.read_text(encoding="utf-8", errors="replace"))
            if m and not re.search(r"Steamworks Common|Redistributable|Proton|Steam Linux Runtime", m.group(1)):
                games.add(m.group(1))
    return sorted(games)


def gt_etc_file_count(_ctx, _arg=None) -> list[int]:
    """Files there, without and with hidden ones (Get-ChildItem shows hidden files only with -Force)."""
    root = Path(r"C:\Windows\System32\drivers\etc")
    files = [p for p in root.iterdir() if p.is_file()]
    shown = [p for p in files if not (os.stat(p).st_file_attributes & 2)]
    return sorted({len(shown), len(files)})


def gt_folder_size_bytes(_ctx, path: str) -> int:
    return sum(p.stat().st_size for p in Path(expand(path)).rglob("*") if p.is_file())


def gt_newest_files(_ctx, arg: dict) -> list[list[str]]:
    files = sorted((p for p in Path(expand(arg["dir"])).iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    return [[p.name, p.stem] for p in files[: int(arg.get("n", 5))]]


GT = {name[3:]: fn for name, fn in globals().items() if name.startswith("gt_") and callable(fn)}


# ---------- checks ----------

def number_pattern(n) -> str:
    """1234 -> '1,?234' (thousands separators optional), not part of a longer number."""
    s = str(n)
    if isinstance(n, int) or s.isdigit():
        s = str(int(n))
        groups = []
        while len(s) > 3:
            groups.insert(0, s[-3:])
            s = s[:-3]
        s = ",?".join([s] + groups)
    else:
        s = re.escape(s)
    return rf"(?<![\d.,]){s}(?![\d])"


def alternatives(value) -> list[str]:
    items = value if isinstance(value, (list, tuple, set)) else [value]
    return [number_pattern(v) if isinstance(v, int) or (isinstance(v, str) and v.isdigit()) else re.escape(str(v)) for v in items]


def mentions(answer: str, alt: str) -> bool:
    return bool(re.search(rf"(?<![\w]){re.escape(alt)}(?![\w])", answer, re.I))


UNITS = {"b": 0, "byte": 0, "bytes": 0, "kb": 1, "kib": 1, "kilobytes": 1, "mb": 2, "mib": 2, "megabytes": 2,
         "gb": 3, "gib": 3, "gigabytes": 3, "tb": 4, "tib": 4, "terabytes": 4}


def sizes_in(answer: str, default_unit: str = "") -> list[tuple[float, int, int]]:
    """(value, unit power of 1000/1024, decimals) for every number in the answer that has a size unit."""
    out = []
    for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(bytes?|[kmgt]i?b|kilobytes|megabytes|gigabytes|terabytes)?\b", answer, re.I):
        unit = (m.group(2) or default_unit).lower()
        if unit in UNITS:
            raw = m.group(1).replace(",", "")
            try:
                out.append((float(raw), UNITS[unit], len(raw.split(".")[1]) if "." in raw else 0))
            except ValueError:
                pass
    return out


def times_in(answer: str) -> list[int]:
    """Minutes of the day for every h:mm in the answer (both readings when there's no am/pm)."""
    out = []
    for m in re.finditer(r"\b(\d{1,2}):(\d{2})(?::\d{2})?\s*([ap])?\.?\s*m?\b", answer, re.I):
        h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
        if h > 23 or mi > 59:
            continue
        if ap:
            h = h % 12 + (12 if ap == "p" else 0)
            out.append(h * 60 + mi)
        else:
            out.append(h * 60 + mi)
            if h <= 12:
                out.append(((h + 12) % 24) * 60 + mi)
    return out


def check(c: dict, run: dict, ctx: dict) -> tuple[bool, str]:
    """One check against one run: (passed, what was observed)."""
    t = c["type"]
    answer = run.get("answer") or ""
    events = run.get("events_full", [])
    tools = [e for e in events if e.get("event") == "tool"]
    director = [e for e in events if e.get("event") == "director"]

    def gt():
        return GT[c["gt"]](ctx, c.get("arg"))

    if t == "answer_nonempty":
        return bool(answer.strip()), f"{len(answer)} chars"
    if t == "answer_regex":
        m = re.search(c["pattern"], answer, re.I)
        return bool(m), f"matched {m.group(0)!r}" if m else f"no match in {answer[:120]!r}"
    if t == "no_answer_regex":
        m = re.search(c["pattern"], answer, re.I)
        return not m, f"found {m.group(0)!r}" if m else "absent"
    if t == "answer_gt":
        value = gt()
        hit = next((a for a in alternatives(value) if re.search(a, answer, re.I)), None)
        return bool(hit), f"gt={value}" + (" found" if hit else f" not in {answer[:120]!r}")
    if t == "answer_gt_bool":
        value = bool(gt())
        yes, no = re.search(c["yes"], answer, re.I), re.search(c["no"], answer, re.I)
        ok = (bool(yes) and not no) if value else bool(no)
        return ok, f"gt={'installed' if value else 'not installed'}; answer says {'no' if no else 'yes' if yes else '?'}"
    if t == "answer_time_near":
        a, b = gt()
        span = c.get("minutes", 2)
        lo, hi = a - span, (b if b >= a else b + 1440) + span
        seen = times_in(answer)
        ok = any(lo <= x <= hi or lo <= x + 1440 <= hi for x in seen)
        return ok, f"gt {a // 60}:{a % 60:02d}..{b // 60}:{b % 60:02d}; answer times {[f'{x // 60}:{x % 60:02d}' for x in seen][:6]}"
    if t == "answer_number_near":
        value = float(gt())
        ok = False
        for v, power, decimals in sizes_in(answer, c.get("unit", "")):
            for base in (1000, 1024):
                got = v * base ** power
                tol = max(c.get("abs_gb", 0) * base ** 3, c.get("rel", 0) * value, 0.5 * 10 ** -decimals * base ** power * 1.01)
                ok = ok or abs(got - value) <= tol
        shown = f"{value / 1e9:.2f} GB" if value > 5e8 else f"{value / 1e6:.2f} MB"
        return ok, f"gt {shown}; answer sizes {sizes_in(answer, c.get('unit', ''))[:5]}"
    if t == "answer_mentions_any":
        groups = gt()
        if not groups and c.get("none"):
            return bool(re.search(c["none"], answer, re.I)), "gt is empty: the answer must say none"
        groups = [g if isinstance(g, list) else [str(g)] for g in groups]
        hit = [g[0] for g in groups if any(mentions(answer, str(x)) for x in g)]
        return len(hit) >= c.get("min", 1), f"{len(hit)}/{len(groups)} mentioned (need {c.get('min', 1)}): {hit[:6]}"
    if t in ("tool_used", "no_tool", "tool_count"):
        def hits(e):
            return ((not c.get("name") or re.search(c["name"], str(e.get("name", "")), re.I))
                    and (not c.get("args") or re.search(c["args"], json.dumps(e.get("args", {}), ensure_ascii=False), re.I))
                    and (not c.get("result") or re.search(c["result"], str(e.get("result", "")), re.I)))
        n = sum(1 for e in tools if hits(e))
        if t == "tool_used":
            return n > 0, f"{n} matching call(s)"
        if t == "no_tool":
            return n == 0, f"{n} matching call(s)"
        ok = n >= c.get("min", 0) and ("max" not in c or n <= c["max"])
        return ok, f"{n} matching call(s)"
    if t == "max_tools":
        return len(tools) <= c["n"], f"{len(tools)} tool call(s)"
    if t == "event_present":
        if c.get("needs_chat") and not run.get("had_conversation"):
            return True, "n/a: no earlier turn in this chat was run"
        n = sum(1 for e in events if e.get("event") == c["event"])
        return n > 0, f"{n} '{c['event']}' event(s)"
    if t == "max_director_rounds":
        return len(director) <= c["n"], f"{len(director)} director round(s)"
    if t == "director_parse_ok":
        bad = [e for e in director if not e.get("actions") and e.get("error")]
        return not bad, f"{len(bad)}/{len(director)} rounds unusable" + (f": {str(bad[0].get('error'))[:80]!r}" if bad else "")
    if t == "director_parse_rate":
        if not director:
            return True, "n/a: no director rounds"
        good = sum(1 for e in director if e.get("actions"))
        return good / len(director) >= c["min"], f"{good}/{len(director)} usable"
    if t == "max_secs":
        if c.get("modes") and ctx["mode"] not in c["modes"]:
            return True, f"n/a in {ctx['mode']} mode"
        return run["secs"] <= c["n"], f"{run['secs']}s"
    if t == "asked":
        qs = run.get("questions", [])
        return any(re.search(c["pattern"], q["question"], re.I) for q in qs), f"{len(qs)} question(s)"
    if t == "status":
        ok = run["status"] == c["is"]
        if ok and c.get("within_secs") is not None:
            ok = run.get("stop_secs") is not None and run["stop_secs"] <= c["within_secs"]
        return ok, f"status {run['status']}" + (f", stopped in {run.get('stop_secs')}s" if run.get("stop_secs") is not None else "")
    if t == "window":
        found = [w for w in windows() if app_match(w, c.get("app"), c.get("title"))]
        if not c.get("exists", True):
            return not found, f"{len(found)} matching window(s)" + (f": {found[0]['title']!r}" if found else "")
        if not found:
            return False, "no matching window"
        if c.get("zoomed") is not None:
            found = [w for w in found if w["zoomed"] == c["zoomed"]]
            if not found:
                return False, "window not maximized" if c["zoomed"] else "window maximized"
        if c.get("text"):
            for w in found:
                text = window_text(w["hwnd"])
                if re.search(c["text"], text, re.I):
                    return True, f"{w['title']!r} shows it"
            return False, f"window text lacks {c['text']!r} ({re.sub(chr(10), ' | ', text)[:120]!r})"
        return True, f"{found[0]['title']!r}"
    if t == "no_new_window":
        new = [w["title"] for w in windows() if w["hwnd"] not in ctx["pre_hwnds"] and w["hwnd"] not in ctx["setup_hwnds"]]
        return not new, f"new: {new[:5]}" if new else "none"
    if t == "file":
        p = Path(expand(c["path"]))
        if not c.get("exists", True):
            return not p.exists(), "exists" if p.exists() else "absent"
        if not p.is_file():
            return False, "missing"
        if c.get("contains"):
            raw = p.read_bytes()
            text = raw.decode("utf-16", "replace") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig", "replace")
            return bool(re.search(c["contains"], text, re.I)), f"contents {text[:60]!r}"
        return True, "exists"
    if t == "no_process":
        new = {pid: n for pid, n in procs(c["pattern"]).items() if pid not in ctx["pre_pids"]}
        return not new, f"new: {sorted(set(new.values()))}" if new else "none started"
    if t == "tools_chars_max":
        start = next((e for e in events if e.get("event") == "start"), {})
        if "tools_chars" not in start:
            return True, "n/a: the start event has no tools_chars yet"
        return start["tools_chars"] <= c["n"], f"{start['tools_chars']} chars"
    return False, f"unknown check type {t}"


# ---------- tasks ----------

def load_tasks() -> list[dict]:
    data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    tasks = data["tasks"]
    problems, ids = [], set()
    for t in tasks:
        tid = t.get("id", "?")
        if tid in ids:
            problems.append(f"{tid}: duplicate id")
        ids.add(tid)
        for key in ("cat", "text", "suites", "checks"):
            if key not in t:
                problems.append(f"{tid}: missing {key}")
        for c in t.get("checks", []):
            if c.get("type") not in CHECK_TYPES:
                problems.append(f"{tid}: unknown check {c.get('type')}")
            if c.get("gt") and c["gt"] not in GT:
                problems.append(f"{tid}: unknown gt {c['gt']}")
            if c.get("app") and c["app"] not in APPS:
                problems.append(f"{tid}: unknown app {c['app']}")
            for key in ("pattern", "name", "args", "result", "yes", "no", "title", "text", "contains", "none"):
                if isinstance(c.get(key), str):
                    try:
                        re.compile(c[key])
                    except re.error as e:
                        problems.append(f"{tid}: bad regex in {c['type']}.{key}: {e}")
        for s in t.get("setup", []):
            if s.get("op") not in SETUP_OPS:
                problems.append(f"{tid}: unknown setup op {s.get('op')}")
            if s.get("app") and s["app"] not in APPS:
                problems.append(f"{tid}: unknown app {s['app']}")
        for app in t.get("requires", {}).get("no_window", []) + t.get("requires", {}).get("window", []):
            if app not in APPS:
                problems.append(f"{tid}: unknown app {app} in requires")
        for td in t.get("teardown", []):
            app = td if isinstance(td, str) else td.get("app")
            if app not in APPS:
                problems.append(f"{tid}: unknown teardown app {app}")
        for a in t.get("answers", []):
            try:
                re.compile(a["match"])
            except (re.error, KeyError) as e:
                problems.append(f"{tid}: bad answer rule: {e}")
    if problems:
        raise SystemExit("tasks.json has problems:\n  " + "\n  ".join(problems))
    return tasks


def pick(tasks: list[dict], suite: str, only: str) -> list[dict]:
    if only:
        want = {w.strip().lower() for w in only.split(",") if w.strip()}
        chosen = [t for t in tasks if t["id"].lower() in want or t["cat"].lower() in want]
        unknown = want - {t["id"].lower() for t in tasks} - {t["cat"].lower() for t in tasks}
        if unknown:
            raise SystemExit(f"no task or category named: {', '.join(sorted(unknown))}")
        return chosen
    return [t for t in tasks if suite in t["suites"]]


def skip_reason(task: dict) -> str:
    wins = windows()
    for app in task.get("requires", {}).get("no_window", []):
        if any(app_match(w, app) for w in wins):
            return f"you have {app} open (the bench never touches your own windows)"
    for app in task.get("requires", {}).get("window", []):
        if not any(app_match(w, app) for w in wins):
            return f"needs a {app} window"
    return ""


def in_sandbox(path: str) -> bool:
    p = re.sub(r"(?i)\$env:(TEMP|TMP)", "%TEMP%", path.strip().strip("'\""))
    try:
        full = os.path.normcase(os.path.abspath(os.path.expandvars(os.path.expanduser(p))))
    except Exception:
        return False
    return full.startswith(os.path.normcase(str(SANDBOX)))


def bench_owned(question: str, ctx: dict) -> bool:
    """Whether a confirmation IO asks for only touches what the bench made: windows created during the task, files in
    the sandbox, processes started during the task. Only then does the bench say yes on its own."""
    new = [w for w in windows() if w["hwnd"] not in ctx["pre_hwnds"]]
    if m := re.search(r"close these windows: (.*?)\. Allow it\?", question, re.S):
        names = [n.strip() for n in m.group(1).split(", ") if n.strip() and n.strip() != "(none match)"]
        return bool(names) and all(any(n == w["title"] for w in new) for n in names)
    if m := re.search(r"(?:write|delete|move) the file (.*?)\. Allow it\?", question, re.S):
        return in_sandbox(m.group(1))
    if m := re.search(r"run PowerShell: (.*)\. Allow it\?", question, re.S):
        cmd = m.group(1)
        paths = re.findall(r"[A-Za-z]:\\[^\s'\";|]+|%TEMP%[^\s'\";|]*|\$env:TE?MP[^\s'\";|]*", cmd, re.I)
        return not RISKY_PS.search(cmd) and bool(paths) and all(in_sandbox(p) for p in paths)
    if m := re.search(r"kill the process (.*?)\. Allow it\?", question, re.S):
        target = m.group(1).strip()
        hits = [p for p in psutil.process_iter(["pid", "name", "create_time"])
                if str(p.info["pid"]) == target or (p.info["name"] or "").lower().removesuffix(".exe") == target.lower().removesuffix(".exe")]
        return bool(hits) and all((p.info["create_time"] or 0) >= ctx["t0"] - 1 for p in hits)
    if question.startswith("Start a loop?"):
        return any(re.search(re.escape(word), question, re.I) for w in new for word in re.findall(r"[A-Za-z]{4,}", w["title"]))
    return False


def answer_for(question: str, task: dict, ctx: dict) -> str:
    for rule in task.get("answers", []):
        if re.search(rule["match"], question, re.I):
            reply = rule["reply"]
            break
    else:  # unscripted: yes only to IO's own confirmations about the bench's own things
        reply = "yes_if_bench" if re.search(r"allow it\?|^start a loop\?", question, re.I) else "never mind"
    if reply == "yes_if_bench":
        reply = "yes" if bench_owned(question, ctx) else "no"
    return reply


def do_setup(task: dict, ctx: dict) -> str:
    """Runs the task's setup ops; returns an error ('' when fine). Windows it opens are recorded as task-owned."""
    for s in task.get("setup", []):
        op = s["op"]
        try:
            if op == "sandbox":
                SANDBOX.mkdir(parents=True, exist_ok=True)
            elif op == "write":
                p = Path(expand(s["path"]))
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(s.get("text", ""), encoding="utf-8")
            elif op == "files":
                d = SANDBOX / s["dir"]
                d.mkdir(parents=True, exist_ok=True)
                days = s.get("mtimes_days_ago") or [0] * len(s["names"])
                for name, ago in zip(s["names"], days):
                    p = d / name
                    p.write_text(f"bench file {name}\n", encoding="utf-8")
                    t = time.time() - float(ago) * 86400
                    os.utime(p, (t, t))
            elif op == "bigfiles":
                d = SANDBOX / s["dir"]
                d.mkdir(parents=True, exist_ok=True)
                total, chunk = int(s["total_mb"] * 1024 * 1024), 4 * 1024 * 1024
                i = 0
                while total > 0:
                    n = min(chunk, total)
                    (d / f"blob-{i}.bin").write_bytes(os.urandom(n))
                    total -= n
                    i += 1
            elif op == "open":
                before = {w["hwnd"] for w in windows()}
                args = OPENERS[s["app"]] + ([expand(s["path"])] if s.get("path") else [])
                import subprocess
                subprocess.Popen(args)
                hwnd = 0
                for _ in range(60):
                    time.sleep(0.25)
                    hwnd = next((w["hwnd"] for w in windows() if w["hwnd"] not in before and app_match(w, s["app"])), 0)
                    if hwnd:
                        break
                if not hwnd:
                    return f"setup couldn't open {s['app']}"
                ctx["setup_hwnds"].add(hwnd)
                time.sleep(1.5)  # let it finish drawing before IO looks at it
            elif op == "maximize":
                hwnd = next((w["hwnd"] for w in windows() if w["hwnd"] in ctx["setup_hwnds"] and app_match(w, s["app"])), 0)
                if not hwnd:
                    return f"setup has no {s['app']} window to maximize"
                l, t, _r, _b = primary_rect()
                user32.ShowWindow(hwnd, 9)  # restore first, so it maximizes on the primary display it's moved to
                user32.SetWindowPos(hwnd, 0, l + 100, t + 100, 1200, 800, 0x0004 | 0x0010)
                user32.ShowWindow(hwnd, 3)
                focus(hwnd)
                time.sleep(0.5)
            elif op == "clip":
                clip_write(s["text"])
        except Exception as e:
            return f"setup {op} failed: {type(e).__name__}: {e}"
    return ""


def do_teardown(task: dict, ctx: dict) -> list[str]:
    """Closes windows created during the task (or by its setup) that the task's teardown names. Returns problems."""
    problems = []
    for td in task.get("teardown", []):
        spec = {"app": td} if isinstance(td, str) else td
        for w in windows():
            if w["hwnd"] in ctx["pre_hwnds"] or not app_match(w, spec["app"], spec.get("title")):
                continue
            if spec["app"] == "notepad":
                err = notepad_close(w["hwnd"], spec.get("discard") or r"^\s*$")
                if err:
                    problems.append(err)
            elif not wm_close(w["hwnd"]):
                problems.append(f"couldn't close {w['title']!r}")
    return problems


def snapshot() -> dict:
    """What the invariants compare against: windows, processes, Chrome tabs, pointer."""
    wins = windows()
    return {
        "windows": wins, "hwnds": {w["hwnd"] for w in wins}, "pids": set(psutil.pids()), "node": node_extension(),
        "edge": headless_edge(), "tabs": Counter(chrome_tabs()), "pointer": pointer(), "t": time.time(),
    }


def invariants(task: dict, pre: dict, ctx: dict, clip_sentinel: str | None) -> list[dict]:
    out = []

    def add(name, ok, detail=""):
        out.append({"name": name, "pass": bool(ok), "detail": detail})

    if clip_sentinel is not None:
        try:
            text, _ = clip_read()
            add("clipboard", text == clip_sentinel, "unchanged" if text == clip_sentinel else f"now {str(text)[:60]!r}")
        except Exception as e:
            add("clipboard", False, f"couldn't read it: {e}")
    if not task.get("loop"):
        x, y = pointer()
        px, py = pre["pointer"]
        add("pointer", abs(x - px) <= 5 and abs(y - py) <= 5, f"moved from {pre['pointer']} to {(x, y)}" if (x, y) != pre["pointer"] else "")
    add("mouse_button_up", not (user32.GetAsyncKeyState(0x01) & 0x8000))
    gone = [w["title"] for w in pre["windows"] if not user32.IsWindow(w["hwnd"])]
    add("your_windows_kept", not gone, f"closed: {gone}" if gone else "")
    left = [w["title"] for w in windows() if w["hwnd"] not in pre["hwnds"]]
    add("no_leftover_windows", not left, f"still open (not touched): {left}" if left else "")
    pattern = STALE_TABS + "".join("|" + re.escape(t) for t in task.get("opens_titles", []))
    for _ in range(10):  # IO's MCP servers and tabs take a moment to go away after the task
        new_tabs = [n for n, k in (Counter(chrome_tabs()) - pre["tabs"]).items() if re.search(pattern, n, re.I)]
        node_now, edge_new = node_extension(), headless_edge() - pre["edge"]
        if not new_tabs and len(node_now) <= len(pre["node"]) and not edge_new:
            break
        time.sleep(1)
    add("no_leftover_tabs", not new_tabs, f"new tabs: {new_tabs}" if new_tabs else "")
    add("no_extra_extension_servers", len(node_now) <= len(pre["node"]), f"{len(pre['node'])} -> {len(node_now)}")
    add("no_headless_edge", not edge_new, f"new: {sorted(edge_new)}" if edge_new else "")
    return out


# ---------- drivers ----------

def trim_event(e: dict, n: int = 300) -> dict:
    out = {}
    for k, v in e.items():
        if isinstance(v, str):
            out[k] = v[:n]
        elif isinstance(v, (list, dict)):
            s = json.dumps(v, ensure_ascii=False)
            out[k] = v if len(s) <= n else s[:n]
        else:
            out[k] = v
    return out


def chrome_token() -> str:
    try:
        return json.loads((REPO / "data" / "browser.json").read_text(encoding="utf-8")).get("chrome_token", "")
    except (OSError, ValueError):
        return ""


def base_options(cell: dict, task: dict, boss) -> dict:
    """Like app.worker builds them: your saved settings, IO's Chrome token, plus this cell's model mode and director."""
    try:
        settings = json.loads((REPO / "data" / "store.json").read_text(encoding="utf-8"))["settings"]
    except (OSError, ValueError, KeyError):
        settings = {}
    defaults = {"allow_powershell": True, "confirm_risky": True, "browser": True, "files": True, "browser_mode": "edge"}
    options = {k: settings.get(k, v) for k, v in defaults.items()}
    options["chrome_token"] = chrome_token()
    options["max_steps"] = settings.get("max_steps", 30)  # what app.worker gives a task (run_direct takes it out again)
    options["model_mode"] = cell["mode"]
    if cell["director"]:
        # nim: GLM-5.3 Flash with Duck.ai behind it (director_order from your settings); duck: Duck.ai alone
        options.update(ask_gemini=True, gemini_mode=cell.get("ai", "nim"), advisor_role="director",
                       director_order=settings.get("director_order", "glm_first"))
    else:
        options.update(ask_gemini=False, gemini_mode=settings.get("gemini_mode", "private"), advisor_role=settings.get("advisor_role", "director"))
    options["focus_glow"] = False
    options["images_from_earlier"] = False
    options["loop"] = bool(task.get("loop") or boss.LOOP_REQUEST.search(task["text"]))
    return options


def conversation_of(earlier: list[dict], boss) -> list[dict]:
    """Earlier turns of the same bench chat, the way app.chat_context hands them to the agent."""
    turns = []
    for r in earlier:
        acts = [f"{e.get('name')}({json.dumps(e.get('args', {}), ensure_ascii=False)[:80]})"
                for e in r.get("events_full", []) if e.get("event") == "tool"][-8:]
        asked = r["text"] + ("\n(context, not part of the request: to answer this you used " + "; ".join(acts) + ")" if acts else "")
        turns.append({"role": "user", "content": asked})
        turns.append({"role": "assistant", "content": boss.clean_summary(r.get("answer") or "") or f"({r['status']})"})
    return turns[-20:]


def error_text(e: BaseException) -> str:
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    return f"{type(e).__name__}: {e}"


async def run_direct(task: dict, cell: dict, ctx: dict, earlier: list[dict], timeout: float, boss_log) -> dict:
    import boss
    events: list[dict] = []
    questions: list[dict] = []

    async def ask(question: str) -> str:
        reply = answer_for(question, task, ctx)
        questions.append({"question": question[:400], "reply": reply, "at": round(time.time() - ctx["t0"], 1)})
        return reply

    def collect(record: dict) -> None:
        events.append(dict(record))

    options = base_options(cell, task, boss)
    conversation = conversation_of(earlier, boss)
    boss.listeners.append(collect)
    run = {"status": "", "answer": "", "error": "", "stop_secs": None, "had_conversation": bool(conversation)}
    ctx["t0"] = time.time()
    max_steps = int(task.get("max_steps") or options.pop("max_steps", 0) or 30)
    quiet = contextlib.ExitStack()
    if boss_log:  # boss.py's log lines and Windows-MCP's stderr go to boss.log, so the bench's own lines stay readable
        quiet.enter_context(contextlib.redirect_stdout(boss_log))
        quiet.enter_context(contextlib.redirect_stderr(boss_log))
    try:
        with quiet:
            job = asyncio.ensure_future(boss.run(task["text"], max_steps, options, ask, conversation, []))
            limit = task.get("run_secs") or timeout
            done, _ = await asyncio.wait({job}, timeout=limit)
            if not done:  # Stop pressed (run_secs) or out of time: cancel it the way app.stop_task does
                t_stop = time.time()
                job.cancel()
                done, _ = await asyncio.wait({job}, timeout=60)
                run["stop_secs"] = round(time.time() - t_stop, 1) if done else None
                run["status"] = "cancelled" if task.get("run_secs") else "timeout"
                if not done:
                    run["error"] = "the task didn't stop within 60s of being cancelled"
            if done and not run["status"]:
                try:
                    run["answer"] = job.result() or ""
                    run["status"] = "done"
                except asyncio.CancelledError:
                    run["status"] = "cancelled"
                except BaseException as e:
                    run["status"], run["error"] = "error", error_text(e)
            elif done:
                with contextlib.suppress(BaseException):
                    job.result()
    finally:
        with contextlib.suppress(ValueError):
            boss.listeners.remove(collect)
    run["secs"] = round(time.time() - ctx["t0"], 1)
    run["events_full"], run["questions"] = events, questions
    return run


async def run_http(task: dict, cell: dict, ctx: dict, chats: dict, timeout: float) -> dict:
    """Through the running app: a chat per bench chat key (a fresh one for tasks without), answering its questions."""
    key = task.get("chat") or f"_{task['id']}_{time.time()}"
    if key not in chats:
        chats[key] = (await asyncio.to_thread(api, "/api/chats", {}))["id"]
    ctx["t0"] = time.time()
    tid = (await asyncio.to_thread(api, f"/api/chats/{chats[key]}/messages", {"text": task["text"], "loop": bool(task.get("loop")), "ultracode": False}))["task_id"]
    run = {"status": "", "answer": "", "error": "", "stop_secs": None, "questions": [], "had_conversation": bool(task.get("chat"))}
    answered, t_stop, t = set(), None, {}
    limit = task.get("run_secs") or timeout
    while True:
        await asyncio.sleep(1)
        state = await asyncio.to_thread(io_state)
        if state is None:
            continue
        t = next((x for x in state["tasks"] if x["id"] == tid), {})
        status = t.get("status", "")
        if status == "waiting" and t.get("question") and (t["question"], len(run["questions"])) not in answered:
            reply = answer_for(t["question"], task, ctx)
            answered.add((t["question"], len(run["questions"])))
            run["questions"].append({"question": t["question"][:400], "reply": reply, "at": round(time.time() - ctx["t0"], 1)})
            with contextlib.suppress(Exception):
                await asyncio.to_thread(api, f"/api/tasks/{tid}/answer", {"answer": reply})
        if status in ("done", "error", "cancelled"):
            run["status"] = status
            if t_stop:
                run["stop_secs"] = round(time.time() - t_stop, 1)
            if status == "cancelled" and not task.get("run_secs"):
                run["status"] = "timeout" if t_stop else "cancelled"
            break
        if t_stop is None and time.time() - ctx["t0"] >= limit:
            t_stop = time.time()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(api, f"/api/tasks/{tid}/stop", {})
        if t_stop and time.time() - t_stop > 60:
            run["status"], run["error"] = "timeout", "the task didn't stop within 60s of Stop"
            break
    run["secs"] = round(time.time() - ctx["t0"], 1)
    run["answer"] = t.get("summary", "") if run["status"] == "done" else ""
    if run["status"] == "error":
        run["error"] = t.get("summary", "")
    run["events_full"] = t.get("events", [])
    return run


# ---------- the run ----------

def wait_idle(limit: float = 900) -> bool:
    """Waits until IO has nothing queued, running or waiting on you. False if it stayed busy (or True if IO is down)."""
    end, told = time.time() + limit, False
    while time.time() < end:
        state = io_state()
        if state is None or not io_busy(state):
            return True
        if not told:
            say(f"  waiting for IO to finish: {io_busy(state)[0]}")
            told = True
        time.sleep(5)
    return False


def set_mode(mode: str, wait: bool = True) -> bool:
    """Switches IO's model mode and waits until both models report ready 3 times in a row (and at least 20 s)."""
    api("/api/settings", {"model_mode": mode})
    if not wait:
        return True
    t0, streak = time.time(), 0
    while time.time() - t0 < 900:
        time.sleep(2)
        st = (io_state() or {}).get("status", {})
        streak = streak + 1 if st.get("boss") == "ready" and st.get("eyes") == "ready" else 0
        if streak >= 3 and time.time() - t0 >= 20:
            return True
    return False


def director_note(r: dict) -> str:
    """' (NimDirector/GLM-5.3 Flash, 58s)': who answered the director rounds and their total time ('' without rounds)."""
    via = sorted(set(v for v in r.get("director_via") or [] if v))
    return f" ({', '.join(via)}, {sum(s or 0 for s in r.get('director_secs') or []):.0f}s)" if via else ""


def summarize(results: list[dict], cells: list[str]) -> dict:
    """Per cell: per-task passes/runs/stable/median secs/rounds, and per-category pass rate and medians."""
    out = {}
    for ck in cells:
        runs = [r for r in results if r["cell"] == ck and not r.get("skipped")]
        tasks: dict = {}
        for r in runs:
            t = tasks.setdefault(r["task"], {"cat": r["cat"], "passes": 0, "runs": 0, "secs": [], "rounds": []})
            t["runs"] += 1
            t["passes"] += int(r["pass"])
            t["secs"].append(r["secs"])
            t["rounds"].append(r["director_rounds"])
        cats: dict = {}
        for tid, t in tasks.items():
            t["stable"] = t["passes"] == t["runs"]
            t["median_secs"] = round(statistics.median(t["secs"]), 1)
            t["median_rounds"] = round(statistics.median(t["rounds"]), 1)
            c = cats.setdefault(t["cat"], {"tasks": 0, "passed": 0, "secs": [], "rounds": []})
            c["tasks"] += 1
            c["passed"] += int(t["passes"] > 0)
            c["secs"] += t.pop("secs")
            c["rounds"] += t.pop("rounds")
        for c in cats.values():
            c["pass_rate"] = round(c["passed"] / c["tasks"], 3)
            c["median_secs"] = round(statistics.median(c.pop("secs")), 1)
            c["median_rounds"] = round(statistics.median(c.pop("rounds")), 1)
        out[ck] = {"tasks": tasks, "cats": cats}
    return out


def gate(results: list[dict], summary: dict, baseline: dict | None) -> list[str]:
    """Reasons the run fails the ship gate (empty = passes)."""
    fails = []
    for r in results:
        if r.get("skipped"):
            continue
        for inv in r["invariants"]:
            if not inv["pass"]:
                fails.append(f"{r['cell']} {r['task']}#{r['repeat']}: invariant {inv['name']} ({inv['detail']})")
        for c in r["checks"]:
            if c["policy"] and not c["pass"]:
                fails.append(f"{r['cell']} {r['task']}#{r['repeat']}: policy {c['type']} ({c['observed']})")
    for ck, cell in summary.items():
        base = (baseline or {}).get("cells", {}).get(ck)
        if not base:
            continue
        for tid, t in cell["tasks"].items():
            if base["tasks"].get(tid, {}).get("stable") and t["passes"] == 0:
                fails.append(f"{ck} {tid}: was stable in the baseline, failed every run now")
        for cat, c in cell["cats"].items():
            b = base["cats"].get(cat)
            if not b:
                continue
            if c["pass_rate"] < b["pass_rate"]:
                fails.append(f"{ck} {cat}: pass rate {c['pass_rate']:.0%} < baseline {b['pass_rate']:.0%}")
            if c["median_secs"] > b["median_secs"] * 1.25 + 2:
                fails.append(f"{ck} {cat}: median {c['median_secs']}s > baseline {b['median_secs']}s x1.25+2")
            if c["median_rounds"] > b["median_rounds"] * 1.2 + 1:
                fails.append(f"{ck} {cat}: median director rounds {c['median_rounds']} > baseline {b['median_rounds']} x1.2+1")
    return fails


def write_reports(path: Path, meta: dict, results: list[dict], summary: dict, fails: list[str]) -> tuple[Path, Path]:
    jpath = path if path.suffix.lower() == ".json" else path / "report.json"
    jpath.parent.mkdir(parents=True, exist_ok=True)
    clean = [{k: v for k, v in r.items() if k != "events_full"} for r in results]
    jpath.write_text(json.dumps({"meta": meta, "summary": summary, "gate": {"pass": not fails, "fails": fails}, "runs": clean},
                                ensure_ascii=False, indent=1), encoding="utf-8")
    md = [f"# IO bench {meta['stamp']}", "",
          f"Driver **{meta['driver']}**, suite **{meta['suite']}**, repeat {meta['repeat']}, cells: {', '.join(meta['cells'])}. "
          f"Took {meta.get('secs', 0) / 60:.1f} min.", ""]
    for w in meta.get("warnings", []):
        md.append(f"- warning: {w}")
    for ck in meta["cells"]:
        md += ["", f"## {ck}", "", "| task | cat | checks | invariants | secs | tools | director | failing checks / notes |",
               "|---|---|---|---|---|---|---|---|"]
        for r in [r for r in results if r["cell"] == ck]:
            if r.get("skipped"):
                md.append(f"| {r['task']} | {r['cat']} | skip | | | | | {r['reason']} |")
                continue
            inv_ok = all(i["pass"] for i in r["invariants"])
            notes = [f"{c['type']}: {c['observed']}" for c in r["checks"] if not c["pass"]]
            notes += [f"invariant {i['name']}: {i['detail']}" for i in r["invariants"] if not i["pass"]]
            if r.get("error"):
                notes.append(f"error: {r['error']}")
            if r.get("teardown_problems"):
                notes += r["teardown_problems"]
            cell_text = "; ".join(notes).replace("|", "/").replace("\n", " ")[:400]
            md.append(f"| {r['task']}#{r['repeat']} | {r['cat']} | {'PASS' if r['pass'] else 'FAIL'} | {'ok' if inv_ok else 'FAIL'} | {r['secs']} | "
                      f"{r['tools']} | {r['director_rounds']}{director_note(r)} | {cell_text} |")
        cats = summary.get(ck, {}).get("cats", {})
        if cats:
            md += ["", "| category | passed | median secs | median director rounds |", "|---|---|---|---|"]
            for cat, c in sorted(cats.items()):
                md.append(f"| {cat} | {c['passed']}/{c['tasks']} | {c['median_secs']} | {c['median_rounds']} |")
    md += ["", "## Gate", "", "**PASS**" if not fails else "**FAIL**"]
    md += [f"- {f}" for f in fails]
    if not meta.get("baseline"):
        md.append("\n(No baseline given: only invariants and policy checks were gated.)")
    mpath = jpath.with_suffix(".md")
    mpath.write_text("\n".join(md) + "\n", encoding="utf-8")
    return jpath, mpath


def save_baseline(summary: dict, stamp: str) -> None:
    try:
        base = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        base = {"cells": {}}
    base["cells"].update(summary)  # cells from separate runs accumulate
    base["saved"] = stamp
    BASELINE_FILE.write_text(json.dumps(base, indent=1), encoding="utf-8")


def dry_run(tasks: list[dict]) -> int:
    say(f"tasks.json: {len(tasks)} tasks OK ({sum('smoke' in t['suites'] for t in tasks)} in smoke)")
    ctx = {"t0": time.time(), "t1": time.time(), "pre_windows": windows(), "mode": "fast"}
    for name, fn in GT.items():
        try:
            arg = {"app_installed": "runescape", "folder_size_bytes": str(HERE), "newest_files": {"dir": str(HERE), "n": 3}}.get(name)
            say(f"  gt {name}({arg or ''}) = {str(fn(ctx, arg))[:150]}")
        except Exception as e:
            say(f"  gt {name} FAILED: {type(e).__name__}: {e}")
            return 1
    t = time.time()
    snap = snapshot()
    say(f"  snapshot in {time.time() - t:.2f}s: {len(snap['windows'])} windows, {sum(snap['tabs'].values())} Chrome tabs, "
        f"{len(snap['node'])} extension servers, {len(snap['edge'])} headless Edge, pointer {snap['pointer']}")
    for w in snap["windows"]:
        say(f"    {w['proc']:<24} {w['title'][:70]!r}".encode("ascii", "replace").decode())
    text, formats = clip_read()
    say(f"  clipboard: {'text' if text is not None else 'no text'}, {len(formats)} format(s)")
    for task in tasks:
        reason = skip_reason(task)
        if reason:
            say(f"  {task['id']} would be skipped: {reason}")
    state = io_state()
    say(f"  IO: {'unreachable' if state is None else state['status']}")
    return 0


async def main_async(args) -> int:
    tasks = pick(load_tasks(), args.suite, args.only)
    if args.dry_run:
        return dry_run(tasks)
    if not tasks:
        raise SystemExit("no tasks selected")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.report) if args.report else HERE / "results" / stamp
    out_dir = out.parent if out.suffix.lower() == ".json" else out
    out_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    state = io_state()
    if state is None:
        warnings.append("IO isn't reachable: running without pausing it (and the http driver can't run)")
        if args.driver == "http":
            raise SystemExit("the http driver needs IO running")
    current = (state or {}).get("settings", {}).get("model_mode") or json.loads((REPO / "data" / "store.json").read_text(encoding="utf-8"))["settings"].get("model_mode", "fast")
    modes = [current if m == "current" else m for m in (args.mode or args.modes).split(",")]
    if len(modes) > 1 and any(m != current for m in modes) and not args.allow_mode_switch:
        raise SystemExit("several modes need --allow-mode-switch (it reloads IO's models)")
    if any(m != current for m in modes) and state is None:
        raise SystemExit("switching modes needs IO running")
    directors = [d.strip() == "on" for d in args.director.split(",")]
    on = "on" if args.director_ai == "duck" else "on-glm"  # (earlier reports' director-on cells were Duck.ai)
    cells = [{"mode": m, "director": d, "ai": args.director_ai, "key": f"{m}/director-{on if d else 'off'}"} for m in modes for d in directors]
    repeat = args.repeat or (2 if args.suite == "full" and not args.only else 1)

    # your clipboard: saved now, put back however the bench ends
    clip_saved, formats = clip_read()
    if clip_saved is None and formats and not args.clobber_clipboard:
        raise SystemExit("your clipboard holds something that isn't text (an image or files); the bench would lose it. "
                         "Copy some text first, or pass --clobber-clipboard.")
    if clip_saved is not None and set(formats) - {1, 7, 13, 16}:
        warnings.append("the clipboard's text is restored at the end, but not its formatting")
    if clip_saved is not None:
        RESTORE.add("clipboard", lambda: clip_write(clip_saved))
    else:  # it was empty (or you allowed clobbering): don't leave a bench sentinel in it
        RESTORE.add("clipboard", clip_clear)

    # IO: idle, then paused for the run (direct driver), restored at the end
    if state is not None:
        if not wait_idle():
            raise SystemExit("IO stayed busy for 15 minutes; not starting")
        if args.driver == "direct":
            was_paused = bool((io_state() or state)["status"].get("paused"))
            api("/api/pause", {"paused": True})
            RESTORE.add("IO's paused state", lambda: api("/api/pause", {"paused": was_paused}))
        if any(m != current for m in modes):
            RESTORE.add("IO's model mode", lambda: set_mode(current, wait=False))
    http_settings = {}
    if args.driver == "http" and state is not None:
        http_settings = {k: state["settings"].get(k) for k in ("ask_gemini", "gemini_mode", "advisor_role")}
        RESTORE.add("IO's director settings", lambda: api("/api/settings", http_settings))

    boss_log = None if args.verbose else open(out_dir / "boss.log", "a", encoding="utf-8")
    results: list[dict] = []
    chats: dict = {}
    t_run = time.time()
    meta = {"stamp": stamp, "driver": args.driver, "suite": args.only or args.suite, "repeat": repeat, "cells": [c["key"] for c in cells],
            "baseline": args.baseline or "", "warnings": warnings, "sandbox": str(SANDBOX), "io_mode_at_start": current}
    say(f"IO bench {stamp}: {len(tasks)} task(s) x {len(cells)} cell(s) x {repeat}, driver {args.driver}; report in {out_dir}")
    stopped = ""
    try:
        for cell in cells:
            if state is not None and cell["mode"] != ((io_state() or {}).get("settings", {}).get("model_mode")):
                say(f"switching IO to {cell['mode']} mode (reloads the models)...")
                if not set_mode(cell["mode"]):
                    warnings.append(f"{cell['key']}: the models weren't ready 15 minutes after switching; cell skipped")
                    continue
            if args.driver == "http":
                api("/api/settings", {"ask_gemini": True, "gemini_mode": cell["ai"], "advisor_role": "director"} if cell["director"] else {"ask_gemini": False})
            if cell["director"] and args.driver == "direct" and cell["ai"] == "duck" and not chrome_token():
                warnings.append(f"{cell['key']}: no Chrome token in data/browser.json, so the director (Duck.ai in Chrome) can't connect")
            if cell["director"] and cell["ai"] == "nim" and not (REPO / "data" / "nim_key.txt").is_file():
                warnings.append(f"{cell['key']}: no NVIDIA key in data/nim_key.txt, so GLM can't answer (Duck.ai and the local model stand in)")
            for rep in range(1, repeat + 1):
                chat_runs: dict[str, list[dict]] = {}
                for task in tasks:
                    rec = {"task": task["id"], "cat": task["cat"], "cell": cell["key"], "repeat": rep, "text": task["text"]}
                    if state is not None and not await asyncio.to_thread(wait_idle):
                        stopped = "IO stayed busy with your tasks for 15 minutes; the bench stopped"
                        break
                    reason = skip_reason(task)
                    if reason:
                        results.append({**rec, "skipped": True, "reason": reason})
                        say(f"  {cell['key']} {task['id']}#{rep}: skipped ({reason})")
                        continue
                    shutil.rmtree(SANDBOX, ignore_errors=True)
                    SANDBOX.mkdir(parents=True, exist_ok=True)
                    pre = snapshot()
                    ctx = {"t0": time.time(), "pre_windows": pre["windows"], "pre_hwnds": pre["hwnds"], "pre_pids": pre["pids"],
                           "setup_hwnds": set(), "mode": cell["mode"]}
                    err = do_setup(task, ctx)
                    sentinel = f"IO-BENCH-CLIP-{len(results) + 1}"
                    try:
                        clip_write(sentinel)
                    except Exception as e:
                        sentinel = None
                        warnings.append(f"{task['id']}: couldn't set the clipboard sentinel: {e}")
                    pre["pointer"] = pointer()  # after setup: the setup itself may focus windows
                    if err:
                        run = {"status": "setup_failed", "answer": "", "error": err, "secs": 0, "events_full": [], "questions": [], "stop_secs": None}
                    else:
                        timeout = float(args.timeout or task.get("timeout") or 300)
                        say(f"  {cell['key']} {task['id']}#{rep}: {task['text'][:70]}")
                        try:
                            if args.driver == "direct":
                                run = await run_direct(task, cell, ctx, chat_runs.get(task.get("chat") or "", []) if task.get("chat") else [], timeout, boss_log)
                            else:
                                run = await run_http(task, cell, ctx, chats, timeout)
                        except Exception as e:
                            run = {"status": "error", "answer": "", "error": f"harness: {error_text(e)}", "secs": round(time.time() - ctx["t0"], 1),
                                   "events_full": [], "questions": [], "stop_secs": None}
                            traceback.print_exc(file=sys.__stderr__)
                    ctx["t1"] = time.time()
                    checks = []
                    for c in task["checks"]:
                        try:
                            ok, observed = check(c, run, ctx)
                        except Exception as e:
                            ok, observed = False, f"check crashed: {type(e).__name__}: {e}"
                        checks.append({"type": c["type"], "pass": ok, "observed": observed, "policy": c["type"] in POLICY_CHECKS})
                    teardown = do_teardown(task, ctx)
                    inv = invariants(task, pre, ctx, sentinel)
                    events = run.pop("events_full")
                    director = [e for e in events if e.get("event") == "director"]
                    rec.update({
                        "pass": run["status"] not in ("setup_failed",) and all(c["pass"] for c in checks), "status": run["status"],
                        "secs": run["secs"], "answer": (run.get("answer") or "")[:1500], "error": run.get("error", ""), "stop_secs": run.get("stop_secs"),
                        "steps": sum(1 for e in events if e.get("event") == "think"), "tools": sum(1 for e in events if e.get("event") == "tool"),
                        "tool_names": [e.get("name") for e in events if e.get("event") == "tool"],
                        "director_rounds": len(director), "director_parse_failures": sum(1 for e in director if not e.get("actions") and e.get("error")),
                        # who answered each round (the chain falls back GLM -> Duck.ai) and how long it took
                        "director_via": [f"{e.get('via', '')}{'/' + e['model'] if e.get('model') else ''}" for e in director],
                        "director_secs": [e.get("secs") for e in director],
                        "questions": run.get("questions", []), "had_conversation": run.get("had_conversation", False),
                        "plan": next((str(e.get("plan", ""))[:600] for e in events if e.get("event") == "plan"), ""),
                        "redo_checks": [str(e.get("text", ""))[:300] for e in events if e.get("event") == "check"],
                        "tools_chars": next((e.get("tools_chars") for e in events if e.get("event") == "start" and "tools_chars" in e), None),
                        "checks": checks, "invariants": inv, "teardown_problems": teardown,
                        "events": [trim_event(e) for e in events], "events_full": events,
                    })
                    results.append(rec)
                    if task.get("chat"):
                        chat_runs.setdefault(task["chat"], []).append(rec)
                    bad = [c["type"] for c in checks if not c["pass"]] + [f"inv:{i['name']}" for i in inv if not i["pass"]]
                    via = sorted(set(rec["director_via"]))
                    say(f"    {'PASS' if rec['pass'] else 'FAIL'} {run['status']} {run['secs']}s, {rec['tools']} tools, {rec['director_rounds']} director"
                        + (f" ({', '.join(via)}; {sum(s or 0 for s in rec['director_secs']):.0f}s)" if via else "")
                        + (f"  failed: {', '.join(bad)}" if bad else "") + (f"  error: {rec['error'][:120]}" if rec["error"] else ""))
                    if args.driver == "direct" and state is not None:
                        with contextlib.suppress(Exception):  # keep IO paused even if you pressed Resume meanwhile
                            if not io_state()["status"].get("paused"):
                                warnings.append("IO was resumed during the bench; paused it again")
                                api("/api/pause", {"paused": True})
                if stopped:
                    break
            if stopped:
                break
    finally:
        for chat_id in chats.values():
            with contextlib.suppress(Exception):
                api(f"/api/chats/{chat_id}", method="DELETE")
        if any(m != current for m in modes) and state is not None and (io_state() or {}).get("settings", {}).get("model_mode") != current:
            say(f"switching IO back to {current} mode...")
            with contextlib.suppress(Exception):
                set_mode(current)
        RESTORE.run()
        shutil.rmtree(SANDBOX, ignore_errors=True)
        if boss_log:
            boss_log.close()
    if stopped:
        warnings.append(stopped)
    meta["secs"] = round(time.time() - t_run)
    summary = summarize(results, [c["key"] for c in cells])
    baseline = None
    if args.baseline:
        try:
            baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            warnings.append(f"couldn't read the baseline {args.baseline}: {e}")
    fails = gate(results, summary, baseline)
    jpath, mpath = write_reports(out, meta, results, summary, fails)
    if args.save_baseline:
        save_baseline(summary, stamp)
        say(f"baseline saved to {BASELINE_FILE}")
    ran = [r for r in results if not r.get("skipped")]
    say(f"\n{sum(r['pass'] for r in ran)}/{len(ran)} runs passed, {len(results) - len(ran)} skipped. Gate: {'PASS' if not fails else 'FAIL'}")
    for f in fails[:20]:
        say(f"  - {f}")
    say(f"report: {mpath}\n        {jpath}")
    return 0 if not fails and not stopped else 1


def main() -> None:
    p = argparse.ArgumentParser(description="IO regression benchmark (bench/tasks.json)")
    p.add_argument("--suite", choices=["smoke", "full"], default="smoke")
    p.add_argument("--only", "--tasks", dest="only", default="", help="comma list of task ids and/or categories (overrides --suite)")
    p.add_argument("--driver", choices=["direct", "http"], default="direct")
    p.add_argument("--mode", default="", help="fast|balanced|smart|current: one model mode (IO is switched to it and back)")
    p.add_argument("--modes", default="current", help="comma list of modes; more than one needs --allow-mode-switch")
    p.add_argument("--allow-mode-switch", action="store_true")
    p.add_argument("--director", default="off,on", help="off, on, or off,on")
    p.add_argument("--director-ai", choices=["nim", "duck"], default="nim",
                   help="director-on cells: nim = GLM-5.3 Flash with Duck.ai behind it (default), duck = Duck.ai alone")
    p.add_argument("--repeat", type=int, default=0, help="runs per task (default 1, or 2 for the full suite)")
    p.add_argument("--timeout", type=float, default=0, help="seconds per task, overriding tasks.json")
    p.add_argument("--report", default="", help="report folder, or a .json path (the .md goes next to it)")
    p.add_argument("--baseline", default="", help="compare against this baseline (e.g. bench\\baseline.json)")
    p.add_argument("--save-baseline", action="store_true", help="write this run's numbers to bench/baseline.json")
    p.add_argument("--clobber-clipboard", action="store_true", help="run even if the clipboard holds non-text data")
    p.add_argument("--dry-run", action="store_true", help="validate tasks.json, compute every ground truth and the invariant snapshot")
    p.add_argument("--verbose", action="store_true", help="show boss.py's own log lines instead of writing them to boss.log")
    args = p.parse_args()
    if args.director.replace(" ", "") not in ("off", "on", "off,on", "on,off"):
        raise SystemExit("--director must be off, on, or off,on")
    sys.path.insert(0, str(REPO))
    os.chdir(REPO)
    try:
        code = asyncio.run(main_async(args))
    except KeyboardInterrupt:
        say("stopped; restoring your clipboard and IO's state")
        RESTORE.run()
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
