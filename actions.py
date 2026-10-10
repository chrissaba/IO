"""IO's action library: higher-level actions (L2/L3) that pick their own method, check their own result and never raise.
boss.py wires the registry into the tools, the director's catalog, the planner and the local model's prompt.

Every result reads one of three ways, so any decider can act on it without guessing:
    ok: <what happened, with the real values> [via <method>] [| now: <state>]
    unsure: <what was done> but <what IO could not confirm> | try: <action(args)>
    error:<CODE>: <what failed, what IO saw> | try: <action(args)>

    python actions.py --selftest     read-only checks plus Notepad/Calculator round trips on windows it opens itself
"""
from __future__ import annotations

import ast
import asyncio
import base64
import builtins
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import difflib
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import warnings
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).parent
HOST = None  # the boss module, set by bind(): its helpers (quick_click, ps_wrap, open_windows, ...) are used, never copied
SETTINGS_FILE = HERE / "data" / "actions.json"
DONE_URL_DEFAULT = "http://127.0.0.1:8765/static/io-done.html"
builtins_max = builtins.max  # several actions take a max= argument, which shadows max() inside them


def bind(host) -> None:
    """boss.py's last line: actions.bind(sys.modules[__name__]). No import of boss at module level, so no cycle."""
    global HOST
    HOST = host


def _h():
    """The bound boss module; a lazy import only if nothing bound it (scratch scripts)."""
    global HOST
    if HOST is None:
        HOST = sys.modules.get("boss")
        if HOST is None:
            import boss  # noqa: PLC0415 - lazy on purpose (see bind)
            HOST = boss
    return HOST


# ======================================================================================================================
# contract
# ======================================================================================================================

CODES = {"NOT_FOUND", "AMBIGUOUS", "NO_CHANGE", "COVERED", "NOT_FOCUSED", "DISABLED", "TIMEOUT", "ELEVATED",
         "UNSUPPORTED", "BLOCKED", "REFUSED", "BAD_ARGS", "NEEDS", "FAILED",  # FAILED: a program ran and exited non-zero
         "STALE"}  # STALE: a file changed on disk since this task read it (an edit from the old read would undo that change)
RESULT = re.compile(r"^(ok: |unsure: |error:(" + "|".join(sorted(CODES)) + r"): )", re.S)


def ok(text: str, via: str = "", now: str = "") -> str:
    return "ok: " + text + (f" via {via}" if via else "") + (f" | now: {now}" if now else "")


def unsure(text: str, try_: str = "") -> str:
    return "unsure: " + text + (f" | try: {try_}" if try_ else "")


def err(code: str, text: str, try_: str = "") -> str:
    return f"error:{code if code in CODES else 'UNSUPPORTED'}: " + text + (f" | try: {try_}" if try_ else "")


class Fail(Exception):
    """Raised inside an action to return an error result from deep inside a helper: call() turns it into err()."""

    def __init__(self, code: str, text: str, try_: str = "") -> None:
        super().__init__(text)
        self.result = err(code, text, try_)


class UiaTimeout(Exception):
    pass


ALL_MODES = frozenset({"single", "loop", "director", "local"})
GROUPS = ["WIN", "READ", "ACT", "SEE", "FILE", "PC", "WEB", "DO", "GAME", "PHONE", "END", "RAW"]
GROUP_HEAD = {
    "WIN": "WIN (windows and apps; exact, under 0.2s unless launching)",
    "READ": "READ (no side effects; UI Automation text, vision only in read_region/check_screen)",
    "ACT": "ACT (UI Automation first, vision fallback; each checks the window changed)",
    "SEE": "SEE (vision: slower, can miss; prefer READ/ACT on normal apps)",
    "FILE": "FILE (no UI; writes only under the user's folders or %TEMP%; nothing is ever deleted for good)",
    "PC": "PC (facts and harmless controls; no model-written commands)",
    "WEB": "WEB (IO's own browser tab only; always Google)",
    "DO": "DO (whole jobs in one action; on failure says at which step)",
    "GAME": "GAME (loop window only; never Esc/Back; never ads or purchases)",
    "PHONE": "PHONE (the iPhone simulator on the Mac, over SSH: look first, then tap/type/swipe; never purchases or sign-ins)",
    "END": "END",
    "RAW": "RAW (low level; coordinates only from list_controls/find_on_screen)",
}


@dataclass
class Action:
    name: str
    level: int
    group: str                       # WIN READ ACT SEE FILE PC WEB DO GAME END RAW
    summary: str                     # <= 70 chars, written for models, never truncated
    params: dict                     # JSON-schema properties
    required: tuple
    fn: Callable | None              # async (ctx, **args) -> str; None = external (boss executes it)
    cost: float                      # typical seconds on this PC, fast mode
    tier: str                        # exact | uia | vision | web | llm
    limits: str = ""                 # one line, shown by tools(group)
    risky: Callable | None = None    # (args, ctx) -> reason or ""
    modes: frozenset = ALL_MODES     # subset of {"single", "loop", "director", "local"}, or {"internal"}
    star: bool = False               # listed in the director's top-level catalog
    expect: bool = False             # accepts expect=
    desc: str = ""                   # extra tool-schema text beyond the summary (PowerShell's two recipes)
    hide: tuple = ()                 # rarely needed params left out of the local model's schema (still accepted)
    top: str | None = None           # the argument list shown in the top-level catalog (None: required args)
    bang: bool = False               # "!" in the catalog: asks the user first
    fallback: str = ""               # the L1 way to do it, named when this action is disabled
    timeout: float = 30.0            # hard cap for one call

    def signature(self, full: bool = True, skip: tuple = ()) -> str:
        if not full and self.top is not None:
            return f"{self.name}({self.top})"
        names = [k + ("" if k in self.required else "?") for k in self.params if (full or k in self.required) and k not in skip]
        return f"{self.name}({','.join(names)})"


REGISTRY: dict[str, Action] = {}


def _params(spec: str) -> tuple[dict, tuple]:
    """Compact parameter lines -> (JSON-schema properties, required). One per line: `name type[?] [a|b|c] description`.
    Types: s string, i integer, n number, b boolean, a list of strings, o object."""
    types = {"s": "string", "i": "integer", "n": "number", "b": "boolean", "a": "array", "o": "object"}
    props, required = {}, []
    for line in spec.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        name, typ, *rest = line.split(None, 2)
        rest = " ".join(rest)
        optional = typ.endswith("?")
        prop: dict = {"type": types[typ.rstrip("?")]}
        if prop["type"] == "array":
            prop["items"] = {"type": "string"}
        m = re.match(r"(\S+\|\S+)\s*(.*)", rest)
        if m:
            prop["enum"] = m.group(1).split("|")
            rest = m.group(2)
        if rest:
            prop["description"] = rest[:40]
        props[name] = prop
        if not optional:
            required.append(name)
    return props, tuple(required)


def action(name: str, *, level: int = 2, group: str, summary: str, params: str = "", cost: float = 1.0, tier: str = "exact",
           **meta):
    """Registers an async fn(ctx, **args) -> str as a native action."""
    def deco(fn):
        # a helper written between @action and its function would take the action's place (run_command once ran a
        # string helper and failed every call): the names have to agree
        if fn.__name__.rstrip("_") != name:
            raise RuntimeError(f"@action({name!r}) decorates {fn.__name__}()")
        props, req = _params(params)
        REGISTRY[name] = Action(name, level, group, summary, props, req, fn, cost, tier, **meta)
        return fn
    return deco


def external(name: str, *, level: int = 1, group: str, summary: str, params: str = "", cost: float = 1.0, tier: str = "exact",
             **meta) -> None:
    """Registers an L1 tool boss already executes, so catalogs, menus and the planner list come from one place."""
    props, req = _params(params)
    REGISTRY[name] = Action(name, level, group, summary, props, req, None, cost, tier, **meta)


def native(name: str) -> bool:
    a = REGISTRY.get(name)
    return bool(a and a.fn is not None)


# --- rollback switch: data/actions.json = {"enabled": true, "disabled": ["click", ...]} ---

_settings_cache: dict = {"t": 0.0, "v": {"enabled": True, "disabled": []}}


def settings() -> dict:
    """The rollback switch, re-read at most every 5 s. A missing or broken file means enabled with nothing disabled."""
    if time.time() - _settings_cache["t"] > 5:
        try:
            v = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            v = {"enabled": bool(v.get("enabled", True)), "disabled": [str(x) for x in v.get("disabled") or []]}
        except (OSError, ValueError, AttributeError):
            v = {"enabled": True, "disabled": []}
        _settings_cache.update(t=time.time(), v=v)
    return _settings_cache["v"]


def enabled() -> bool:
    return settings()["enabled"]


def disabled(name: str) -> bool:
    return name in settings()["disabled"]


# ======================================================================================================================
# per-task context
# ======================================================================================================================

@dataclass
class Ctx:
    """Built once per task by boss._run (package D)."""
    options: dict = field(default_factory=dict)
    win: Any = None                     # Windows-MCP ClientSession
    browser: Any | None = None          # Playwright MCP ClientSession (IO's tab)
    browser_mode: str = "edge"
    eyes: Any = None                    # boss.Eyes
    ask: Callable | None = None         # async (question) -> answer
    loop: bool = False
    focus: str = ""                     # loop window lock (part of a title)
    request: str = ""                   # the resolved request text
    constraints: list = field(default_factory=list)
    opened: set = field(default_factory=set)        # hwnds IO opened in this task
    dialogs: set = field(default_factory=set)       # dialogs IO opened (an abandoned Save As, ...)
    tab_open: bool = False                          # IO's browser tab exists in this task
    found_points: list = field(default_factory=list)  # shared with boss's loop click guard
    research: Callable | None = None                # async (question) -> str
    hud: list = field(default_factory=list)         # game_state history
    log: Callable = print
    fails: dict = field(default_factory=dict)       # call key -> consecutive failures (rut detection)
    last_key: str = ""
    game_cache: dict = field(default_factory=dict)
    allowed: Callable | None = None                 # (name) -> bool: what this task may run (toggles, a loop's window lock)
    page_chars: int = 6000                          # read_page's length: boss raises it to fit a large-context brain
    written: set = field(default_factory=set)       # files this task created (lowercase paths): its own to overwrite
    # lowercase path -> (size, mtime_ns, sha1) of each text file as read_file last showed it (and after IO's own writes
    # to it): edit_file / write_file refuse with STALE when the file changed on disk since, instead of undoing that change
    file_prints: dict = field(default_factory=dict)


# what models write for an enum value -> the value (each wrong spelling cost the director a whole round)
ENUM_ALIASES = {
    "maximise": "max", "maximize": "max", "maximised": "max", "maximized": "max", "maximum": "max", "full": "max", "fullscreen": "max",
    "minimise": "min", "minimize": "min", "minimised": "min", "minimized": "min", "hide": "min",
    "normal": "restore", "restored": "restore", "unmaximize": "restore", "foreground": "front", "focus": "front", "bottom": "back",
    "settled": "screen_still", "screen_settled": "screen_still", "settle": "screen_still", "still": "screen_still", "idle": "screen_still",
    "stable": "screen_still", "loaded": "screen_still", "changed": "screen_changes", "change": "screen_changes",
    "text": "text_appears", "text_visible": "text_appears", "appears": "text_appears", "visible": "text_appears",
    "gone": "text_gone", "disappears": "text_gone", "window": "window_appears", "window_open": "window_appears",
    "window_closed": "window_gone", "enabled": "element_enabled", "file": "file_exists",
    "dont_save": "discard", "don't_save": "discard", "no_save": "discard", "nosave": "discard",
}


def _coerce(a: Action, args: dict) -> tuple[dict, str]:
    """Arguments as the schema wants them (small models send "true", "3", "a, b" and JSON text). Returns (args, problem)."""
    out = {}
    for k, v in (args or {}).items():
        spec = a.params.get(k)
        if spec is None:
            out[k] = v  # unknown extras are passed through and ignored by **_
            continue
        t = spec["type"]
        try:
            if v is None:
                continue
            if t == "integer" and not isinstance(v, bool):
                v = int(float(v))
            elif t == "number" and not isinstance(v, bool):
                v = float(v)
            elif t == "boolean" and not isinstance(v, bool):
                v = str(v).strip().lower() in ("1", "true", "yes", "y", "on")
            elif t == "array" and not isinstance(v, list):
                if isinstance(v, str) and v.strip().startswith("["):
                    v = json.loads(v)
                else:
                    v = [s.strip() for s in str(v).split(",") if s.strip()] if isinstance(v, str) else [v]
            elif t == "object" and isinstance(v, str):
                v = json.loads(v)
            elif t == "string" and not isinstance(v, str):
                v = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
        except (TypeError, ValueError):
            return out, f"{k} should be {t}"
        if "enum" in spec and isinstance(v, str) and v not in spec["enum"]:
            low = v.strip().lower()
            alias = ENUM_ALIASES.get(low.replace(" ", "_").replace("-", "_"), "")
            hit = ([e for e in spec["enum"] if e.lower() == low] or [e for e in spec["enum"] if e == alias]
                   or [e for e in spec["enum"] if e.lower().startswith(low)])
            if not hit:
                return out, f"{k} must be one of {'|'.join(spec['enum'])}"
            v = hit[0]
        out[k] = v
    missing = [k for k in a.required if out.get(k) in (None, "", [], {}) and not (a.params[k]["type"] == "boolean" and k in out)]
    if missing:
        return out, "missing " + ", ".join(missing)
    return out, ""


async def call(name: str, args: dict, ctx: Ctx) -> str:
    """Validate, check disabled, time, run, catch, format. Never raises (except Stop's CancelledError)."""
    a = REGISTRY.get(name)
    if a is None:
        close = difflib.get_close_matches(name, list(REGISTRY), n=2)
        return err("BAD_ARGS", f"no action named {name!r}", " or ".join(f"{c}()" for c in close) or 'tools("ACT")')
    if a.fn is None:
        return err("UNSUPPORTED", f"{name} is run by IO itself, not the action library")
    if disabled(name):
        return err("UNSUPPORTED", "disabled", a.fallback or "another action")
    args, problem = _coerce(a, args if isinstance(args, dict) else {})
    if problem:
        return err("BAD_ARGS", problem, a.signature())
    key = name + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    n_failed = ctx.fails.get(key, 0) if ctx.last_key == key else 0
    if n_failed >= 2:
        return err("REFUSED", "same call failed 3 times; stop trying this", a.fallback or "another action, or done")
    expect = str(args.pop("expect", "") or "") if not a.expect else str(args.get("expect", "") or "")
    t0 = time.time()
    try:
        cap = a.timeout + (float(args.get("timeout") or 0) if name == "wait_until" else 0)
        result = await asyncio.wait_for(a.fn(ctx, **args), cap)
    except asyncio.CancelledError:
        raise  # Stop must stop
    except Fail as e:
        result = e.result
    except (UiaTimeout, asyncio.TimeoutError):
        result = err("TIMEOUT", f"{name} took longer than allowed (the window may be hung)", "another way, or ask_user")
    except Exception as e:  # tools never raise into the loop
        result = err("UNSUPPORTED", f"{type(e).__name__}: {str(e)[:200]}")
    if not isinstance(result, str) or not RESULT.match(result):
        result = ok(str(result)) if isinstance(result, str) and not result.lower().startswith("error") else \
            err("UNSUPPORTED", str(result)[:300])
    if expect and a.expect and result.startswith("ok:"):
        result = await _check_expect(ctx, args, expect, result)
    failed = result.startswith("error") or result.startswith("unsure")
    ctx.fails[key] = (n_failed + 1) if failed else 0
    ctx.last_key = key
    if failed and n_failed == 1:  # after the prefix, so "unsure:" / "error:CODE:" still lead
        result = re.sub(r"^(unsure:|error:\w+:)", r"\1 (2nd time)", result)
    # a failure is usually one line; but a program that exited non-zero (FAILED) or was stopped (TIMEOUT) carries its own
    # output, already cut to fit by its action, and the error is in it: cut at 700, a failing build or test run showed
    # the brain its first error line and nothing of what it needed to fix it
    if failed and len(result) > (24000 if result.startswith(("error:FAILED", "error:TIMEOUT")) else 700):
        result = result[:24000 if result.startswith(("error:FAILED", "error:TIMEOUT")) else 700] + "…"
    if ctx.options.get("debug_actions"):
        ctx.log(f"[actions] {name} {round(time.time() - t0, 2)}s {result[:120]!r}")
    return result


# ======================================================================================================================
# infrastructure: win32, windows, guards, input, signatures, COM thread, UIA tree
# ======================================================================================================================

# own WinDLL instances: setting argtypes/restype here must not change the shared ctypes.windll functions boss uses
_u = ctypes.WinDLL("user32", use_last_error=True)
_k = ctypes.WinDLL("kernel32", use_last_error=True)
_dwm = ctypes.WinDLL("dwmapi")
_ENUM = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
_u.EnumChildWindows.argtypes = [wt.HWND, _ENUM, wt.LPARAM]
_u.EnumWindows.argtypes = [_ENUM, wt.LPARAM]
_u.VkKeyScanW.restype = ctypes.c_short
_u.GetForegroundWindow.restype = wt.HWND
_u.GetWindow.restype = wt.HWND
_u.GetWindow.argtypes = [wt.HWND, wt.UINT]
_u.GetAncestor.restype = wt.HWND
_u.GetAncestor.argtypes = [wt.HWND, wt.UINT]
_u.WindowFromPoint.restype = wt.HWND
_u.WindowFromPoint.argtypes = [wt.POINT]
_u.IsWindow.argtypes = [wt.HWND]
_u.IsWindowVisible.argtypes = [wt.HWND]
_u.IsIconic.argtypes = [wt.HWND]
_u.IsZoomed.argtypes = [wt.HWND]
_u.IsWindowEnabled.argtypes = [wt.HWND]
_u.GetWindowTextLengthW.argtypes = [wt.HWND]
_u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_u.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_u.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
_u.SetForegroundWindow.argtypes = [wt.HWND]
_u.BringWindowToTop.argtypes = [wt.HWND]
_u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
_u.MonitorFromWindow.restype = wt.HMONITOR
_u.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
_u.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
_u.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
_k.OpenProcess.restype = wt.HANDLE
_k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
_k.CloseHandle.argtypes = [wt.HANDLE]

EMULATORS = {"hd-player.exe", "bluestacks.exe", "nox.exe", "ldplayer.exe", "dnplayer.exe", "memu.exe", "mumuplayer.exe"}
BROWSERS = {"chrome.exe": "chrome", "msedge.exe": "edge", "firefox.exe": "firefox", "opera.exe": "opera", "brave.exe": "brave"}
PROTECTED = {"claude.exe": "claude", "discord.exe": "discord"}
# words for an app -> the exe(s) whose windows are that app (the title alone often doesn't say: Calculator, Explorer)
APP_EXES = {
    "notepad": ["notepad.exe"], "calculator": ["calculatorapp.exe", "calculator.exe", "calc.exe"], "calc": ["calculatorapp.exe", "calc.exe"],
    "file explorer": ["explorer.exe"], "explorer": ["explorer.exe"], "files": ["explorer.exe"], "settings": ["systemsettings.exe"],
    "paint": ["mspaint.exe"], "word": ["winword.exe"], "excel": ["excel.exe"], "powerpoint": ["powerpnt.exe"], "outlook": ["outlook.exe", "olk.exe"],
    "chrome": ["chrome.exe"], "edge": ["msedge.exe"], "firefox": ["firefox.exe"], "bluestacks": ["hd-player.exe"],
    "terminal": ["windowsterminal.exe"], "task manager": ["taskmgr.exe"], "vs code": ["code.exe"], "code": ["code.exe"],
    "spotify": ["spotify.exe"], "steam": ["steam.exe", "steamwebhelper.exe"], "discord": ["discord.exe"], "photos": ["photos.exe"],
    "snipping tool": ["snippingtool.exe"], "clock": ["time.exe"], "control panel": ["control.exe"],
}
SKIP_TITLES = {"Program Manager", "Windows Input Experience", "Taskbar", "NVIDIA GeForce Overlay", "Microsoft Text Input Application"}
# titles of IO's own browser pages (the Duck.ai chat is named after the task: "Open document window"); a browser window
# showing one is IO's, never the user's document or the app a task works in (boss adds and removes them)
OWN_PAGE_TITLES: set = set()
BROWSER_SUFFIX = re.compile(r"\s+[-–—]\s+(Google Chrome|Microsoft​? Edge|Mozilla Firefox|Brave|Opera)$")


def own_page(title: str) -> bool:
    """A browser window that is showing one of IO's own pages (its Duck.ai chat), by its title."""
    m = BROWSER_SUFFIX.search(title or "")
    if not m:
        return False
    page = title[:m.start()].strip()
    return page.startswith("Duck.ai") or page in OWN_PAGE_TITLES


@dataclass
class W:
    hwnd: int
    title: str
    pid: int
    exe: str          # lower-case file name of the process that owns the window (the real app for UWP frames)
    cls: str
    rect: tuple

    @property
    def minimized(self) -> bool:
        return bool(_u.IsIconic(self.hwnd))

    @property
    def maximized(self) -> bool:
        return bool(_u.IsZoomed(self.hwnd))


def _text(hwnd) -> str:
    n = _u.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _u.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _cls(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _u.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _rect(hwnd) -> tuple:
    r = wt.RECT()
    _u.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def _pid(hwnd) -> int:
    pid = wt.DWORD()
    _u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


_exe_cache: dict = {}


def _exe_of_pid(pid: int) -> str:
    if pid in _exe_cache:
        return _exe_cache[pid]
    name = ""
    h = _k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(1024)
            if _k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value).lower()
        finally:
            _k.CloseHandle(h)
    if len(_exe_cache) > 500:
        _exe_cache.clear()
    _exe_cache[pid] = name
    return name


def _cloaked(hwnd) -> bool:
    """Suspended UWP frames and windows on other virtual desktops are 'visible' but cloaked (not on screen)."""
    val = ctypes.c_int(0)
    try:
        _dwm.DwmGetWindowAttribute(wt.HWND(hwnd), 14, ctypes.byref(val), ctypes.sizeof(val))  # DWMWA_CLOAKED
    except OSError:
        return False
    return bool(val.value)


def _app_exe(hwnd, pid: int) -> str:
    """The real app behind a window: UWP apps (Calculator, Settings) live in an ApplicationFrameHost frame whose
    CoreWindow child belongs to the app's own process."""
    exe = _exe_of_pid(pid)
    if exe != "applicationframehost.exe":
        return exe
    found: list = []

    def cb(child, _):
        cp = _pid(child)
        if cp != pid:
            found.append(_exe_of_pid(cp))
            return False
        return True

    _u.EnumChildWindows(hwnd, _ENUM(cb), 0)
    return found[0] if found and found[0] else exe


def windows(owned: bool = False) -> list[W]:
    """Visible, uncloaked top-level windows with titles, front to back. Includes Settings (boss.open_windows skips it).
    owned=True also lists owned popups (dialogs)."""
    out: list[W] = []

    def cb(hwnd, _):
        try:
            if not _u.IsWindowVisible(hwnd) or (not owned and _u.GetWindow(hwnd, 4)):  # GW_OWNER
                return True
            title = _text(hwnd)
            if not title or title in SKIP_TITLES or _cloaked(hwnd) or own_page(title):
                return True
            pid = _pid(hwnd)
            out.append(W(int(hwnd), title, pid, _app_exe(hwnd, pid), _cls(hwnd), _rect(hwnd)))
        except Exception:
            pass
        return True

    _u.EnumWindows(_ENUM(cb), 0)
    return out


def _w(hwnd) -> W | None:
    if not hwnd or not _u.IsWindow(hwnd):
        return None
    pid = _pid(hwnd)
    return W(int(hwnd), _text(hwnd), pid, _app_exe(hwnd, pid), _cls(hwnd), _rect(hwnd))


def fg() -> W | None:
    return _w(_u.GetForegroundWindow())


def _alive(hwnd) -> bool:
    return bool(hwnd and _u.IsWindow(hwnd) and _u.IsWindowVisible(hwnd))


def _norm(s: str) -> str:
    """Names compared the way people say them: no &accelerators, no '...', no tab-separated shortcut, no colon."""
    s = (s or "").split("\t")[0].replace("&", "").replace("…", "").replace("...", "")
    return re.sub(r"\s+", " ", s).strip().rstrip(":").strip().lower()


def resolve(ctx: Ctx | None, window: str = "") -> W | None:
    """A window by part of its title, resolved once to an hwnd: exact title, starts with, contains, then the app's exe.
    Empty means the task's window: ctx.focus, then boss's focus hint, then the foreground window."""
    window = str(window or "").strip()
    if not window:
        hint = (ctx.focus if ctx else "") or getattr(HOST, "focus_hint", "") or ""
        if hint and (w := resolve(ctx, hint)):
            return w
        w = fg()
        return w if w and w.title not in SKIP_TITLES and not own_page(w.title) else None
    m = re.fullmatch(r"(?:hwnd[:=])?(\d{3,})", window)
    if m and _u.IsWindow(int(m.group(1))):
        return _w(int(m.group(1)))
    wins = windows()
    low = window.lower()
    # a few titles that read like app words: "notepad" must not resolve to a Chrome tab titled "notepad tips"
    exes = APP_EXES.get(low) or APP_EXES.get(low.removesuffix(" app")) or []
    tiers = [
        [w for w in wins if w.title.lower() == low],
        [w for w in wins if exes and w.exe in exes],
        [w for w in wins if w.title.lower().startswith(low)],
        [w for w in wins if low in w.title.lower()],
        [w for w in wins if w.exe.removesuffix(".exe") == low.replace(" ", "")],
        [w for w in windows(owned=True) if low in w.title.lower()],
    ]
    opened = ctx.opened if ctx else set()
    for tier in tiers:
        if tier:
            tier.sort(key=lambda w: (w.hwnd not in opened, w.minimized))  # IO's own windows, then visible ones (stable: z-order)
            return tier[0]
    return None


def _need(ctx: Ctx, window: str) -> W:
    w = resolve(ctx, window)
    if w is None:
        titles = ", ".join(f"'{x.title[:40]}'" for x in windows()[:8])
        raise Fail("NOT_FOUND", f"no window matching {window!r}" if window else "no window to work in", f"list_windows() (open: {titles})")
    return w


def _named(ctx: Ctx, *words: str) -> bool:
    req = (ctx.request or "").lower() if ctx else ""
    return any(w and re.search(r"\b" + re.escape(w.lower()) + r"\b", req) for w in words)


def _is_io(w: W) -> bool:
    return w.title == "IO" or w.pid == os.getpid()


def guard_input(ctx: Ctx, w: W) -> None:
    """Input never goes to Claude, Discord, IO's own panel or a browser window unless the request names that app
    (IO works on web pages only through its own tab)."""
    if _is_io(w) and not _named(ctx, "io panel"):
        raise Fail("BLOCKED", "that is IO's own window", "work in another window")
    if "unsloth" in w.exe or "llama-server" in w.title.lower() or "unsloth" in w.title.lower():
        raise Fail("BLOCKED", "that window runs IO's own models", "work in another window")
    app = PROTECTED.get(w.exe) or BROWSERS.get(w.exe)
    if app and not _named(ctx, app, "browser" if w.exe in BROWSERS else app):
        how = "web_search(...) or read_page(url)" if w.exe in BROWSERS else "ask_user"
        raise Fail("BLOCKED", f"'{w.title[:50]}' is the user's {app} window, not part of this request", how)


def guard_read(ctx: Ctx, w: W) -> None:
    app = PROTECTED.get(w.exe)
    if (_is_io(w) and not _named(ctx, "io")) or (app and not _named(ctx, app)):
        raise Fail("BLOCKED", f"'{w.title[:50]}' is private to the user", "ask_user")


def _owner_chain(hwnd) -> set:
    out, h = set(), hwnd
    for _ in range(6):
        if not h:
            break
        out.add(int(h))
        h = _u.GetWindow(h, 4)  # GW_OWNER
    return out


def _is_front(hwnd) -> bool:
    """The window, or a dialog it owns, has the keyboard."""
    f = _u.GetForegroundWindow()
    return bool(f) and int(hwnd) in _owner_chain(f)


def _focus_sync(hwnd) -> bool:
    """Bring a window to the front by hwnd (titles can repeat: two 'Untitled - Notepad'), with the same input nudge as
    boss.focus_window, then AttachThreadInput, then minimise and restore."""
    if not _u.IsWindow(hwnd):
        return False
    if _is_front(hwnd) and not _u.IsIconic(hwnd):
        return True  # already there: a stray Alt press would light up the menu bar's access keys and take the keyboard
    if _u.IsIconic(hwnd):
        _u.ShowWindow(hwnd, 9)  # SW_RESTORE
    # Windows only lets the process that sent the last input hand over focus; a tap of an unassigned key (0x97) satisfies
    # that. (An Alt here landed its key-up in the window being focused: Win11 Notepad's key tips then ate the typing.)
    _u.keybd_event(0x97, 0, 0, 0)
    _u.keybd_event(0x97, 0, 2, 0)
    _u.SetForegroundWindow(hwnd)
    _u.BringWindowToTop(hwnd)
    _u.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)  # HWND_TOP, no move/size, show
    for attempt in range(3):
        time.sleep(0.08)
        if _is_front(hwnd):
            return True
        if attempt == 0:
            fg_thread = _u.GetWindowThreadProcessId(_u.GetForegroundWindow(), None)
            me = _k.GetCurrentThreadId()
            _u.AttachThreadInput(me, fg_thread, True)
            try:
                _u.SetForegroundWindow(hwnd)
                _u.BringWindowToTop(hwnd)
            finally:
                _u.AttachThreadInput(me, fg_thread, False)
        elif attempt == 1:
            zoomed = _u.IsZoomed(hwnd)
            _u.ShowWindow(hwnd, 6)  # SW_MINIMIZE
            time.sleep(0.15)
            _u.ShowWindow(hwnd, 3 if zoomed else 9)
            _u.SetForegroundWindow(hwnd)
    time.sleep(0.1)
    return _is_front(hwnd)


async def focus(w: W) -> bool:
    return await asyncio.to_thread(_focus_sync, w.hwnd)


def _top_at(hwnd, pt) -> bool:
    """Whether a click at pt lands in this window (or one of its own dialogs), not in one covering it."""
    under = _u.WindowFromPoint(wt.POINT(int(pt[0]), int(pt[1])))
    root = _u.GetAncestor(under, 2) if under else None  # GA_ROOT
    return bool(root) and (int(root) == int(hwnd) or int(hwnd) in _owner_chain(root) or _pid(root) == _pid(hwnd))


async def _uncovered(w: W, pt) -> None:
    if not await asyncio.to_thread(_top_at, w.hwnd, pt):
        await focus(w)
        if not await asyncio.to_thread(_top_at, w.hwnd, pt):
            raise Fail("COVERED", f"another window covers '{w.title[:40]}' at ({pt[0]}, {pt[1]})", "focus_window(...) or dismiss_dialog()")


# --- keyboard and mouse ---

class _KI(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MI(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _HI(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _IU(ctypes.Union):
    _fields_ = [("ki", _KI), ("mi", _MI), ("hi", _HI)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _IU)]


_u.SendInput.argtypes = [wt.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
VK = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B, "enter": 0x0D, "return": 0x0D,
      "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
      "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22, "up": 0x26, "down": 0x28,
      "left": 0x25, "right": 0x27, "space": 0x20, "printscreen": 0x2C, "apps": 0x5D, "menu": 0x5D, "capslock": 0x14,
      "plus": 0xBB, "minus": 0xBD, "comma": 0xBC, "period": 0xBE, "back": 0xA6, "browser_back": 0xA6,
      "volume_up": 0xAF, "volume_down": 0xAE, "volume_mute": 0xAD, "play_pause": 0xB3, "next": 0xB0, "prev": 0xB1}
VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})
_EXTENDED = {0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x5B, 0x5D, 0xA6, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB3}


def _key_input(vk: int = 0, scan: int = 0, flags: int = 0) -> _INPUT:
    inp = _INPUT(type=1)
    inp.u.ki = _KI(vk, scan, flags | (1 if vk in _EXTENDED else 0), 0, 0)
    return inp


def _send(inputs: list) -> None:
    for i in range(0, len(inputs), 60):  # batches: very long SendInput arrays get dropped by some apps
        chunk = inputs[i:i + 60]
        arr = (_INPUT * len(chunk))(*chunk)
        _u.SendInput(len(chunk), arr, ctypes.sizeof(_INPUT))
        time.sleep(0.008)


def _vk_of(key: str) -> int | None:
    key = key.strip().lower()
    if key in VK:
        return VK[key]
    if len(key) == 1:
        if key.isalnum():
            return ord(key.upper())
        r = _u.VkKeyScanW(ctypes.c_wchar(key))
        return (r & 0xFF) if r != -1 else None
    return None


def send_combo(combo: str) -> str:
    """Presses a key combination like "ctrl+shift+s" or "enter". Returns '' or what was wrong."""
    parts = [p for p in re.split(r"\s*\+\s*", combo.strip().lower()) if p] if combo.strip() != "+" else ["plus"]
    vks = []
    for p in parts:
        vk = _vk_of(p)
        if vk is None:
            return f"unknown key {p!r}"
        vks.append(vk)
    _send([_key_input(vk) for vk in vks] + [_key_input(vk, flags=2) for vk in reversed(vks)])  # KEYEVENTF_KEYUP
    return ""


def _still_front(hwnd, done: int = 0) -> None:
    """Keys only ever go to the window they are meant for: if the user switched windows, stop (Ctrl+W in their
    browser would close their tab)."""
    if hwnd and not _is_front(hwnd):
        holder = _w(_u.GetForegroundWindow())
        raise Fail("NOT_FOCUSED", f"focus moved to '{holder.title[:40] if holder else '?'}'" + (f" after {done} characters" if done else "")
                   + "; nothing more was typed", "focus_window(...) then try again")


def press(w: W, combo: str) -> None:
    """A key combination for this window only."""
    _still_front(w.hwnd)
    problem = send_combo(combo)
    if problem:
        raise Fail("BAD_ARGS", problem, 'hotkeys(["ctrl+s"])')


def _type_unicode(text: str, pace: float = 0.004, hwnd: int = 0) -> None:
    """One character per SendInput, paced. Windows turns a queued VK_PACKET into the most recently injected character,
    so an app busy for a moment (Notepad after Enter) turned a burst of "more text" into "ttttttttt". With hwnd, it
    stops as soon as that window loses the keyboard."""
    for i, ch in enumerate(text.replace("\r\n", "\n").replace("\r", "\n")):
        if hwnd and i % 20 == 0:
            _still_front(hwnd, i)
        if ch in "\n\t":
            vk = 0x0D if ch == "\n" else 0x09
            _send([_key_input(vk), _key_input(vk, flags=2)])
            time.sleep(0.03)
            continue
        data = ch.encode("utf-16-le")
        units = [int.from_bytes(data[j:j + 2], "little") for j in range(0, len(data), 2)]  # outside the BMP: two surrogates
        arr = (_INPUT * (2 * len(units)))(*[k for u in units for k in (_key_input(0, u, 4), _key_input(0, u, 4 | 2))])
        _u.SendInput(len(arr), arr, ctypes.sizeof(_INPUT))  # KEYEVENTF_UNICODE down + up
        time.sleep(pace)


def _clip_open() -> bool:
    import win32clipboard  # pywin32
    for _ in range(10):  # another app may hold the clipboard for a moment
        try:
            win32clipboard.OpenClipboard()
            return True
        except Exception:
            time.sleep(0.05)
    return False


def clip_state() -> tuple[str, str | None]:
    """('empty' | 'text' | 'other', saved text). 'other' = images, files or rich formats IO can't put back."""
    import win32clipboard
    if not _clip_open():
        return "other", None
    try:
        fmts, f = [], 0
        while True:
            f = win32clipboard.EnumClipboardFormats(f)
            if not f:
                break
            fmts.append(f)
        if not fmts:
            return "empty", None
        plain = {1, 7, 13, 16}  # CF_TEXT, CF_OEMTEXT, CF_UNICODETEXT, CF_LOCALE
        if all(x in plain for x in fmts):
            try:
                return "text", win32clipboard.GetClipboardData(13)
            except Exception:
                return "other", None
        return "other", None
    finally:
        win32clipboard.CloseClipboard()


def clip_set(text: str | None) -> bool:
    import win32clipboard
    if not _clip_open():
        return False
    try:
        win32clipboard.EmptyClipboard()
        if text is not None:
            win32clipboard.SetClipboardData(13, text)
        return True
    finally:
        win32clipboard.CloseClipboard()


def type_text_safe(text: str, enter: bool = False, exe: str = "", hwnd: int = 0) -> str:
    """Types into whatever has the keyboard without touching the user's clipboard: Unicode SendInput up to 300
    characters; longer text (and emulators, which ignore Unicode input) is pasted with the clipboard saved and put back.
    A clipboard holding an image or files is never replaced: then it's Unicode input in chunks. With hwnd, nothing is
    typed once that window loses the keyboard (raises Fail NOT_FOCUSED). Returns the method."""
    how = "keys"
    _still_front(hwnd)
    if len(text) > 300 or exe in EMULATORS:
        state, saved = clip_state()
        if state in ("empty", "text") and clip_set(text):
            try:
                _still_front(hwnd)
                send_combo("ctrl+v")
                time.sleep(0.3)
            finally:
                clip_set(saved)  # back to what the user had (or empty)
            how = "paste (clipboard restored)"
        else:
            for i in range(0, len(text), 200):
                _type_unicode(text[i:i + 200], hwnd=hwnd)
                time.sleep(0.03)
    else:
        _type_unicode(text, hwnd=hwnd)
    if enter:
        time.sleep(0.05)
        _still_front(hwnd)
        send_combo("enter")
    return how


def _cursor() -> tuple[int, int]:
    p = wt.POINT()
    _u.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def mouse_click(x: int, y: int, button: str = "left", clicks: int = 1) -> str:
    """Right and double clicks that put the pointer back afterwards (left single clicks go through boss.quick_click)."""
    if button == "left" and clicks == 1 and HOST is not None:
        return _h().quick_click(int(x), int(y))
    down, up = {"left": (0x2, 0x4), "right": (0x8, 0x10), "middle": (0x20, 0x40)}.get(button, (0x2, 0x4))
    home = _cursor()
    _u.SetCursorPos(int(x), int(y))
    time.sleep(0.03)
    try:
        for i in range(max(1, min(3, clicks))):
            _u.mouse_event(down, 0, 0, 0, 0)
            time.sleep(0.02)
            _u.mouse_event(up, 0, 0, 0, 0)
            time.sleep(0.06)
    finally:
        time.sleep(0.02)
        _u.SetCursorPos(*home)
    return f"{button} {'double ' if clicks == 2 else ''}clicked at ({int(x)},{int(y)})"


def mouse_wheel(x: int, y: int, notches: int) -> None:
    home = _cursor()
    _u.SetCursorPos(int(x), int(y))
    time.sleep(0.03)
    try:
        for _ in range(abs(notches)):
            _u.mouse_event(0x0800, 0, 0, ctypes.c_uint32(120 if notches > 0 else -120).value, 0)  # MOUSEEVENTF_WHEEL
            time.sleep(0.03)
    finally:
        _u.SetCursorPos(*home)


# --- screen and tree signatures ---

def screen_sig(rect) -> Any:
    """The rect as a 96x54 grayscale array: cheap to compare, blind to IO's capture-excluded glow."""
    import numpy as np
    from PIL import ImageGrab
    l, t, r, b = (int(v) for v in rect)
    if r - l < 4 or b - t < 4:
        return None
    img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True).convert("L").resize((96, 54))
    return np.asarray(img, dtype=np.uint8)


def sig_diff(a, b) -> tuple[float, float]:
    """(mean absolute difference, fraction of cells that changed by more than 12)."""
    import numpy as np
    if a is None or b is None or a.shape != b.shape:
        return 255.0, 1.0
    d = np.abs(a.astype(np.int16) - b.astype(np.int16))
    return float(d.mean()), float((d > 12).mean())


def changed(a, b, noise: tuple | None = None) -> bool:
    mean, frac = sig_diff(a, b)
    if noise:  # animated windows: 3x what changes on its own counts as a change
        return frac > max(0.004, 3 * noise[1]) or mean > max(1.5, 3 * noise[0])
    return frac > 0.004 or mean > 1.5


async def sig_of(rect) -> Any:
    try:
        return await asyncio.to_thread(screen_sig, rect)
    except Exception:
        return None


# --- COM thread: all UI Automation runs on one thread with COM initialised ---

def _uia_init() -> None:
    from windows_mcp import uia
    uia.InitializeUIAutomationInCurrentThread()
    uia.SetGlobalSearchTimeout(1)


def _new_uia() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(1, thread_name_prefix="io-uia", initializer=_uia_init)


_UIA = _new_uia()
_tls = threading.local()


async def on_uia(fn, *a, timeout: float = 3.0):
    """Runs fn on the COM thread. A hung provider (common in Electron apps) gets the thread replaced, not waited on."""
    global _UIA
    ex = _UIA
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(loop.run_in_executor(ex, fn, *a), timeout)
    except asyncio.TimeoutError:
        if ex is _UIA:
            _UIA = _new_uia()
            ex.shutdown(wait=False)
        raise UiaTimeout(getattr(fn, "__name__", "uia"))


def uia_sync(fn, *a, timeout: float = 5.0):
    """The same from plain threads (selftest, sync helpers)."""
    return _UIA.submit(fn, *a).result(timeout)


ACTIONABLE = {"Button", "SplitButton", "Edit", "ComboBox", "CheckBox", "RadioButton", "MenuItem", "TabItem", "ListItem", "Hyperlink",
              "Slider", "TreeItem", "DataItem", "Spinner", "Document", "MenuBar"}
KIND_ALIASES = {"button": {"Button", "SplitButton"}, "field": {"Edit", "ComboBox", "Document"}, "edit": {"Edit", "Document"},
                "text": {"Text"}, "link": {"Hyperlink"}, "menu": {"MenuItem"}, "tab": {"TabItem"}, "item": {"ListItem", "DataItem", "TreeItem"},
                "checkbox": {"CheckBox"}, "radio": {"RadioButton"}, "combo": {"ComboBox"}, "dropdown": {"ComboBox"},
                "slider": {"Slider"}, "list": {"List", "DataGrid", "Table", "Tree"}}


class N:
    """One UI Automation element as plain data (Control objects never leave the COM thread)."""
    __slots__ = ("i", "parent", "depth", "name", "kind", "aid", "cls", "rect", "enabled", "offscreen", "password", "value", "ro",
                 "toggle", "expand", "selected", "help", "focus", "pats", "rid")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.rect
        return (l + r) // 2, (t + b) // 2

    def visible_in(self, wrect) -> bool:
        l, t, r, b = self.rect
        return (not self.offscreen and r - l > 1 and b - t > 1 and r > wrect[0] and l < wrect[2] and b > wrect[1] and t < wrect[3])

    def state(self) -> str:
        bits = []
        if self.enabled is False:
            bits.append("disabled")
        if self.toggle is not None and self.kind in ("CheckBox", "Button", "ToggleButton", "ListItem", "MenuItem"):
            bits.append({0: "off", 1: "on", 2: "mixed"}.get(self.toggle, ""))
        if self.selected:
            bits.append("selected")
        if self.expand == 1:
            bits.append("expanded")
        if self.value and self.kind in ("Edit", "ComboBox", "Spinner", "Slider"):
            bits.append(f'value="{"•••" if self.password else self.value[:40]}"')
        return " ".join(b for b in bits if b)


_P = None


def _props():
    global _P
    if _P is None:
        from windows_mcp.uia import PropertyId as P
        _P = P
    return _P


def _cache_request():
    """One cache request per COM thread: the whole subtree's properties come back in a single cross-process call."""
    req = getattr(_tls, "req", None)
    if req is None:
        from windows_mcp import uia
        P = _props()
        req = uia.CacheRequest()
        req.TreeScope = uia.TreeScope.TreeScope_Subtree
        for name in ("NameProperty", "ControlTypeProperty", "AutomationIdProperty", "ClassNameProperty", "BoundingRectangleProperty",
                     "IsEnabledProperty", "IsOffscreenProperty", "IsPasswordProperty", "HelpTextProperty", "HasKeyboardFocusProperty",
                     "ValueValueProperty", "ValueIsReadOnlyProperty", "ToggleToggleStateProperty", "ExpandCollapseExpandCollapseStateProperty",
                     "SelectionItemIsSelectedProperty", "IsInvokePatternAvailableProperty", "IsTogglePatternAvailableProperty",
                     "IsSelectionItemPatternAvailableProperty", "IsExpandCollapsePatternAvailableProperty", "IsValuePatternAvailableProperty",
                     "IsScrollItemPatternAvailableProperty", "IsTextPatternAvailableProperty", "IsRangeValuePatternAvailableProperty",
                     "IsScrollPatternAvailableProperty", "IsWindowPatternAvailableProperty", "IsItemContainerPatternAvailableProperty",
                     "RangeValueValueProperty", "NativeWindowHandleProperty"):
            pid = getattr(P, name, None)
            if pid is not None:
                req.AddProperty(pid)
        _tls.req = req
    return req


_KIND_NAMES: dict = {}
_PAT_PROPS = {"invoke": "IsInvokePatternAvailableProperty", "toggle": "IsTogglePatternAvailableProperty",
              "select": "IsSelectionItemPatternAvailableProperty", "expand": "IsExpandCollapsePatternAvailableProperty",
              "value": "IsValuePatternAvailableProperty", "scrollitem": "IsScrollItemPatternAvailableProperty",
              "text": "IsTextPatternAvailableProperty", "range": "IsRangeValuePatternAvailableProperty",
              "scroll": "IsScrollPatternAvailableProperty", "window": "IsWindowPatternAvailableProperty",
              "container": "IsItemContainerPatternAvailableProperty"}


def _kind(ct: int) -> str:
    if not _KIND_NAMES:
        from windows_mcp import uia
        _KIND_NAMES.update({k: v.removesuffix("Control") for k, v in uia.ControlTypeNames.items()})
    return _KIND_NAMES.get(ct, str(ct))


def _cached(e, pid, typ):
    try:
        v = e.GetCachedPropertyValue(pid)
    except Exception:
        return None
    return v if isinstance(v, typ) and not (typ is int and isinstance(v, bool) and typ is not bool) else None


def walk(hwnd: int, max_nodes: int = 3000, max_depth: int = 25, root_elem=None) -> tuple[list[N], list]:
    """COM thread only. The window's UI Automation tree in document order as plain nodes, plus the raw elements (for
    acting within the same call). One BuildUpdatedCache round trip: 0.02-0.1 s for a normal window."""
    from windows_mcp import uia
    P = _props()
    root = root_elem if root_elem is not None else uia.ControlFromHandle(int(hwnd)).Element
    el = root.BuildUpdatedCache(_cache_request().check_request)
    nodes: list[N] = []
    elems: list = []
    stack = [(el, -1, 0)]
    pat_ids = {k: getattr(P, v, None) for k, v in _PAT_PROPS.items()}
    while stack and len(nodes) < max_nodes:
        e, parent, depth = stack.pop()
        try:
            r = e.CachedBoundingRectangle
            rect = (r.left, r.top, r.right, r.bottom)
        except Exception:
            rect = (0, 0, 0, 0)
        try:
            name = (e.CachedName or "").replace("‎", "").replace("‏", "")  # Explorer's invisible direction marks
            ct = e.CachedControlType
        except Exception:
            continue
        pats = {k for k, pid in pat_ids.items() if pid is not None and _cached(e, pid, bool)}
        val = _cached(e, P.ValueValueProperty, str) if "value" in pats else None
        if val:
            val = val.replace("‎", "").replace("‏", "")
        if val is None and "range" in pats:
            rv = _cached(e, P.RangeValueValueProperty, float)
            val = f"{rv:g}" if rv is not None else None
        n = N(i=len(nodes), parent=parent, depth=depth, name=name.strip(), kind=_kind(ct), aid=(_cached(e, P.AutomationIdProperty, str) or ""),
              cls=(_cached(e, P.ClassNameProperty, str) or ""), rect=rect, enabled=_cached(e, P.IsEnabledProperty, bool),
              offscreen=bool(_cached(e, P.IsOffscreenProperty, bool)), password=bool(_cached(e, P.IsPasswordProperty, bool)),
              value=(val or "").strip(), ro=_cached(e, P.ValueIsReadOnlyProperty, bool),
              toggle=_cached(e, P.ToggleToggleStateProperty, int) if "toggle" in pats else None,
              expand=_cached(e, P.ExpandCollapseExpandCollapseStateProperty, int) if "expand" in pats else None,
              selected=_cached(e, P.SelectionItemIsSelectedProperty, bool) if "select" in pats else None,
              help=(_cached(e, P.HelpTextProperty, str) or ""), focus=bool(_cached(e, P.HasKeyboardFocusProperty, bool)), pats=pats)
        nodes.append(n)
        elems.append(e)
        if depth >= max_depth:
            continue
        try:
            arr = e.GetCachedChildren()
        except Exception:
            arr = None
        if arr:
            for j in range(arr.Length - 1, -1, -1):
                stack.append((arr.GetElement(j), n.i, depth + 1))
    return nodes, elems


def control(e):
    """A raw element as a uia Control (for its pattern wrappers). COM thread only."""
    from windows_mcp import uia
    return uia.Control.CreateControlFromElement(e)


def pattern(e, name: str):
    from windows_mcp import uia
    pid = {"invoke": uia.PatternId.InvokePattern, "toggle": uia.PatternId.TogglePattern, "select": uia.PatternId.SelectionItemPattern,
           "expand": uia.PatternId.ExpandCollapsePattern, "value": uia.PatternId.ValuePattern, "scrollitem": uia.PatternId.ScrollItemPattern,
           "text": uia.PatternId.TextPattern, "range": uia.PatternId.RangeValuePattern, "scroll": uia.PatternId.ScrollPattern,
           "window": uia.PatternId.WindowPattern, "container": uia.PatternId.ItemContainerPattern,
           "virtual": uia.PatternId.VirtualizedItemPattern, "legacy": uia.PatternId.LegacyIAccessiblePattern}[name]
    c = control(e)
    return c.GetPattern(pid) if c else None


def live_rect(e) -> tuple:
    r = e.CurrentBoundingRectangle
    return (r.left, r.top, r.right, r.bottom)


def tree_sig_sync(hwnd: int) -> str:
    """sha1 over what a user would notice changing (names, types, enabled, toggles, values) for the first 800 nodes."""
    try:
        nodes, _ = walk(hwnd, max_nodes=800)
    except Exception:
        return ""
    h = hashlib.sha1()
    for n in nodes:
        h.update(f"{n.name}|{n.kind}|{n.enabled}|{n.toggle}|{n.expand}|{n.selected}|{(n.value or '')[:40]}\n".encode("utf-8", "replace"))
    return h.hexdigest()


async def tree_sig(hwnd: int) -> str:
    try:
        return await on_uia(tree_sig_sync, hwnd, timeout=3.0)
    except Exception:
        return ""


async def snapshot(w: W) -> tuple:
    """(tree sig, screen sig) before acting."""
    return await tree_sig(w.hwnd), await sig_of(_rect(w.hwnd))


async def wait_change(w: W, before: tuple, settle: float = 0.35, limit: float = 1.2, new_from: set | None = None) -> str:
    """'' when nothing changed, else what changed: 'tree', 'screen', 'window gone', or "new window 'X'"."""
    await asyncio.sleep(settle)
    t_end = time.time() + max(0.0, limit - settle)
    while True:
        if not _u.IsWindow(w.hwnd):
            return "window gone"
        if new_from is not None:
            new = [x for x in windows(owned=True) if x.hwnd not in new_from]
            if new:
                return f"new window '{new[0].title[:50]}'"
        tsig = await tree_sig(w.hwnd)
        if tsig and before[0] and tsig != before[0]:
            return "tree"
        ssig = await sig_of(_rect(w.hwnd))
        if ssig is not None and before[1] is not None and changed(before[1], ssig):
            return "screen"
        if time.time() >= t_end:
            return ""
        await asyncio.sleep(0.2)


def candidates(nodes: list[N], text: str, kinds: set | None = None, wrect=None, fuzzy: bool = False) -> tuple[list[N], int]:
    """Nodes matching text, best tier first: exact name, starts with, contains, AutomationId, help text, and with
    fuzzy=True a 0.85 look-alike with the same digits ("file-29" never matches "file-02"). Acting actions leave fuzzy
    off: a near name is reported as 'closest', not clicked. Returns (matches of the best tier, tier). Nested copies (a
    button and its own label, same rect) count once."""
    t = _norm(text)
    if not t:
        return [], -1
    digits = re.findall(r"\d+", t)
    tiers: list[list[N]] = [[] for _ in range(6)]
    for n in nodes:
        if kinds and n.kind not in kinds:
            continue
        nm = _norm(n.name)
        if nm == t:
            tiers[0].append(n)
        elif nm and nm.startswith(t):
            tiers[1].append(n)
        elif nm and len(t) >= 3 and t in nm:
            tiers[2].append(n)
        elif n.aid and n.aid.lower() == t:
            tiers[3].append(n)
        elif n.help and len(t) >= 3 and t in n.help.lower():
            tiers[4].append(n)
        elif fuzzy and nm and len(t) >= 4 and re.findall(r"\d+", nm) == digits and difflib.SequenceMatcher(None, nm, t).ratio() >= 0.85:
            tiers[5].append(n)
    for k, tier in enumerate(tiers):
        if not tier:
            continue
        # visible and actionable first; one entry per screen rect (the outer, actionable one)
        tier.sort(key=lambda n: (not (wrect is None or n.visible_in(wrect)), n.kind not in ACTIONABLE, n.enabled is False, n.i))
        seen, out = set(), []
        for n in tier:
            if n.rect in seen and n.rect != (0, 0, 0, 0):
                continue
            seen.add(n.rect)
            out.append(n)
        acts = [n for n in out if n.kind in ACTIONABLE]
        return (acts or out), k
    return [], -1


def closest_names(nodes: list[N], text: str, kinds: set | None = None, n: int = 5) -> str:
    names = list(dict.fromkeys(x.name for x in nodes if x.name and (not kinds or x.kind in kinds) and len(x.name) < 60))
    near = difflib.get_close_matches(text, names, n=n, cutoff=0.4) or [x for x in names if x.lower()[:3] == text.lower()[:3]][:n]
    return ", ".join(f'"{x}"' for x in near)


def label_target(nodes: list[N], label: N, kinds: set) -> N | None:
    """The field a Text label names: the next control of the wanted kind after it, under the same parent or nearby."""
    for n in nodes[label.i + 1: label.i + 8]:
        if n.kind in kinds:
            return n
    return None


def _kinds(kind: str) -> set | None:
    k = (kind or "any").strip().lower()
    if k in ("", "any", "all"):
        return None
    if k in KIND_ALIASES:
        return KIND_ALIASES[k]
    return {k[:1].upper() + k[1:]}


# --- rung memory: what never works for an app is skipped for an hour ---

_rung_fail: dict = {}


def rung_ok(exe: str, act: str, rung: str) -> bool:
    n, since = _rung_fail.get((exe, act, rung), (0, 0.0))
    if n < 3:
        return True
    if time.time() - since > 3600:
        _rung_fail[(exe, act, rung)] = (2, time.time())  # try once more; one more failure skips it again
        return True
    return False


def rung_result(exe: str, act: str, rung: str, worked: bool) -> None:
    if worked:
        _rung_fail.pop((exe, act, rung), None)
    else:
        n, _ = _rung_fail.get((exe, act, rung), (0, 0.0))
        _rung_fail[(exe, act, rung)] = (n + 1, time.time())


# --- loop guards ---

def loop_guard(ctx: Ctx, pt) -> None:
    """In loops every point must lie inside the locked window's content area."""
    if ctx.loop and ctx.focus:
        area = _h().content_rect(ctx.focus)
        if area and not (area[0] <= pt[0] < area[2] and area[1] <= pt[1] < area[3]):
            raise Fail("BLOCKED", f"({pt[0]}, {pt[1]}) is outside the {ctx.focus} content area", "click(target, how=\"vision\")")


def note_point(ctx: Ctx, pt) -> str:
    """Records a vision-found point for boss's don't-guess guard; says so when the same spot keeps coming back."""
    same = sum(1 for x, y in ctx.found_points[-3:] if abs(pt[0] - x) <= 15 and abs(pt[1] - y) <= 15)
    ctx.found_points[:] = (ctx.found_points + [tuple(pt)])[-8:]
    if same >= 2:
        return (" Note: this is the same spot your last searches found. If acting on it didn't do what you wanted, it isn't the "
                "thing you're after: look at the screen and try something else.")
    return ""


# --- PowerShell: Windows-MCP when the task has it, else a local hidden powershell.exe ---

def _q(s: str) -> str:
    """A single-quoted PowerShell literal (no injection: quotes doubled)."""
    return "'" + str(s).replace("'", "''") + "'"


async def ps(ctx: Ctx | None, cmd: str, timeout: int = 30) -> tuple[str, int]:
    """(output text, status code). Output is UTF-8 safe through boss.ps_wrap/ps_unwrap. Only IO's own fixed commands come
    here (values quoted with _q, never model-written), so with the PowerShell tool turned off (allow_powershell, which is
    about the model's commands) they run in the local hidden powershell.exe instead of failing as 'no such tool'."""
    h = _h()
    wrapped = h.ps_wrap(cmd)
    if ctx is not None and ctx.win is not None and ctx.options.get("allow_powershell", True):
        res = await asyncio.wait_for(ctx.win.call_tool("PowerShell", {"command": wrapped, "timeout": timeout}), timeout + 10)
        raw = h.text_of(res)
    else:
        def run() -> str:
            p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", wrapped],
                               capture_output=True, timeout=timeout, creationflags=0x08000000)  # CREATE_NO_WINDOW
            out = p.stdout.decode("utf-8", "replace")
            return out + f"\nStatus Code: {p.returncode}"
        try:
            raw = await asyncio.to_thread(run)
        except subprocess.TimeoutExpired:
            raise Fail("TIMEOUT", f"PowerShell took over {timeout}s")
    text = h.ps_unwrap(raw)
    m = re.match(r"Response: (.*)\n\nStatus Code: (-?\d+)\s*$", text, re.S)
    if m:
        out, code = m.group(1), int(m.group(2))
        return ("" if out == "(no output)" else out.strip()), code
    return text.strip(), 1 if text.lower().startswith("error") else 0


def run_quiet(args: list[str], timeout: float = 10) -> str:
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=0x08000000)
        return p.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


# --- vision helpers (the boss model's own vision, the same capture as Eyes.describe) ---

def _jpeg(img, side: int = 1600) -> str:
    img = img.convert("RGB")
    img.thumbnail((side, side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def vlm(img, prompt: str, max_tokens: int = 200) -> str:
    """One quick question about an image to EvoCUA, the eyes (Glimmer runs without its vision part). It always thinks
    first, so its thinking is capped and the answer still gets max_tokens of room (blocking: run it in a thread)."""
    from openai import OpenAI
    h = _h()
    reply = OpenAI(base_url=h.EVO_URL, api_key="local", max_retries=1, timeout=90).chat.completions.create(
        model=h.EVO_MODEL, temperature=0, max_tokens=max_tokens + h.EVO_THINK["thinking_budget_tokens"] + 50, extra_body=h.EVO_THINK,
        messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": _jpeg(img)}}, {"type": "text", "text": prompt}]}])
    return re.sub(r"<think>.*?</think>", "", reply.choices[0].message.content or "", flags=re.S).strip()


def grab(rect):
    from PIL import ImageGrab
    return ImageGrab.grab(bbox=tuple(int(v) for v in rect), all_screens=True)


def _area_rect(ctx: Ctx, w: W | None, display: int = 0) -> tuple:
    """What vision looks at: the loop's content area, a window, or a display."""
    if w is not None:
        if ctx.loop and ctx.focus and (r := _h().content_rect(ctx.focus)):
            return r
        return _rect(w.hwnd)
    rects = _h().displays()
    return rects[display] if 0 <= display < len(rects) else rects[0]


def eyes(ctx: Ctx):
    if ctx.eyes is None:
        ctx.eyes = _h().Eyes()
    return ctx.eyes


async def yes_no(ctx: Ctx, question: str, w: W | None) -> tuple[str, str]:
    """('yes' | 'no' | '', the model's sentence) about a window or the primary display."""
    if w is not None:
        await focus(w)
        await asyncio.sleep(0.25)
    img = await asyncio.to_thread(grab, _area_rect(ctx, w))
    where = f"the '{w.title[:60]}' window" if w else "the primary display"
    reply = await asyncio.to_thread(vlm, img, f"This is a screenshot of {where}. Answer YES or NO first, then one short reason.\n{question}", 60)
    m = re.match(r"\W*(yes|no)\b[\s.,:;!-]*(.*)", reply, re.I | re.S)
    if not m:
        return "", reply[:200]
    return m.group(1).lower(), m.group(2).strip()[:200]


async def _check_expect(ctx: Ctx, args: dict, expect: str, result: str) -> str:
    """The cheapest check that fits `expect`: a window title, short text in the window, else a yes/no look."""
    t_end = time.time() + 3.0
    target = expect.strip().strip('"\'')
    w = resolve(ctx, str(args.get("window") or ""))
    titles_hit = lambda: [x for x in windows(owned=True) if target.lower() in x.title.lower()]
    if titles_hit():
        return result
    short = expect.strip()[:1] in "\"'" or len(target.split()) <= 6
    while time.time() < t_end:
        if titles_hit():
            return result
        if short and w is not None and _alive(w.hwnd):
            try:
                if target.lower() in await on_uia(_texts_sync, w.hwnd, timeout=4.0):
                    return result
            except Exception:
                pass
        await asyncio.sleep(0.4)
    if not short:
        try:
            ans, why = await yes_no(ctx, f"Does the screen show this: {expect}?", w)
            if ans == "yes":
                return result
            return err("NO_CHANGE", f'expected "{expect[:80]}" but {why or "it is not shown"}', "check_screen(...) or another target")
        except Exception:
            pass
    seen = f"'{w.title[:50]}' does not show it" if w else "it did not appear"
    return err("NO_CHANGE", f'expected "{expect[:80]}" but {seen} ({result[4:120]})', "read_window(...) or check_screen(...)")


def _monitors() -> list[tuple[tuple, tuple]]:
    """(monitor rect, work area) per display, primary first."""
    out: list = []

    class MI(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

    def cb(hmon, _hdc, _r, _d):
        mi = MI()
        mi.cbSize = ctypes.sizeof(mi)
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        m, w = mi.rcMonitor, mi.rcWork
        out.append((bool(mi.dwFlags & 1), (m.left, m.top, m.right, m.bottom), (w.left, w.top, w.right, w.bottom)))
        return True

    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)
    ctypes.windll.user32.EnumDisplayMonitors(None, None, proc(cb), 0)
    out.sort(key=lambda r: (not r[0], r[1][0]))
    return [(m, w) for _, m, w in out]


def _monitor_of(rect) -> int:
    cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
    for i, (m, _) in enumerate(_monitors()):
        if m[0] <= cx < m[2] and m[1] <= cy < m[3]:
            return i + 1
    return 1


def _new_windows(before: set, match: Callable[[W], bool] | None = None) -> list[W]:
    return [w for w in windows(owned=True) if w.hwnd not in before and (match is None or match(w))]


async def _wait_window(before: set, match: Callable[[W], bool], timeout: float, any_new: bool = False) -> W | None:
    """A new window that matches (or, with any_new, the only new one) within timeout. Polls every 150 ms."""
    t_end = time.time() + timeout
    while True:
        new = _new_windows(before)
        hit = [w for w in new if match(w)]
        if hit:
            return hit[0]
        if any_new and len([w for w in new if not _u.GetWindow(w.hwnd, 4)]) == 1 and time.time() > t_end - timeout / 2:
            return [w for w in new if not _u.GetWindow(w.hwnd, 4)][0]
        if time.time() >= t_end:
            return None
        await asyncio.sleep(0.15)


def _app_match(name: str) -> Callable[[W], bool]:
    low = name.lower().strip().removesuffix(".exe")  # "notepad.exe" is Notepad
    exes = APP_EXES.get(low) or APP_EXES.get(low.removesuffix(" app")) or []
    bare = re.sub(r"[^a-z0-9]", "", low)
    browser_named = any(re.search(r"\b" + app + r"\b", low) for app in BROWSERS.values())

    def m(w: W) -> bool:
        if w.exe in BROWSERS and not browser_named and w.exe not in exes:
            return False  # a page titled "Notepad tips" (or IO's own Duck.ai chat, named after the task) isn't the app
        return (w.exe in exes or low in w.title.lower() or (bare and bare in re.sub(r"[^a-z0-9]", "", w.exe.removesuffix(".exe")))
                or (bare and bare in re.sub(r"[^a-z0-9]", "", w.title.lower())))
    return m


# ======================================================================================================================
# L2 · WIN: windows and apps
# ======================================================================================================================

@action("open_app", group="WIN", summary="open or switch to an app by name; checks its window is in front",
        params="""
        name s app name as in the Start menu (Notepad, Calculator)
        new b? true: a new window even if one is open
        expect s? what should show afterwards
        """, cost=2.5, tier="exact", star=True, expect=True, top="name", fallback='App(mode="launch", name)', timeout=25,
        limits="UAC prompts can't be answered (ask_user); Win11 Notepad restores old tabs, so write with write_in_app")
async def open_app(ctx: Ctx, name: str, new: bool = False, **_) -> str:
    low = name.lower().strip()
    for exe, app in BROWSERS.items():
        if re.search(r"\b" + app + r"\b", low) and not _named(ctx, app):
            raise Fail("BLOCKED", f"IO doesn't drive the user's {app}; it has its own browser tab", f'web_search("...") or read_page("https://...")')
    match = _app_match(name)
    before = {w.hwnd for w in windows(owned=True)}
    existing = [w for w in windows() if match(w)]
    if existing and not new:
        w = existing[0]
        guard_input(ctx, w)
        front = await focus(w)
        _h().hint_focus(w.title)
        return ok(f"switched to '{w.title}' (already open)", now="front" if front else f"behind '{(fg() or w).title[:40]}'")
    how = ""
    if ctx.win is not None:
        try:
            res = _h().text_of(await asyncio.wait_for(ctx.win.call_tool("App", {"mode": "launch", "name": name}), 12))
            if "error" not in res.lower()[:80]:
                how = "App launch"
        except Exception:
            pass
    w = await _wait_window(before, match, 6 if how else 0.5, any_new=bool(how)) if how else None
    if w is None:
        # the Start menu's own list: the exact app id, launched through the shell like a Start menu click
        out, _code = await ps(ctx, f"$a = Get-StartApps | Where-Object {{ $_.Name -like {_q('*' + name + '*')} }} | "
                                   "Sort-Object { $_.Name.Length } | Select-Object -First 1; if ($a) { Start-Process "
                                   "('shell:AppsFolder\\' + $a.AppID); $a.Name }", timeout=15)
        if out.strip() and _code == 0 and not out.lstrip().lower().startswith("error"):
            how = f"Start menu '{out.strip().splitlines()[-1]}'"
        elif re.fullmatch(r"[\w +-]{1,40}(\.exe)?", name.strip(), re.I):
            # a bare app name the shell knows (App Paths, PATH: "notepad", "mspaint"); never a path or a script
            try:
                await asyncio.to_thread(os.startfile, name.strip())
                how = "Start-Process"
            except OSError:
                pass
        if not how:
            raise Fail("NOT_FOUND", f"no app called {name!r} in the Start menu", f'app_info("{name}") or find_file("{name}")')
        w = await _wait_window(before, match, 9, any_new=True)
    if w is None:
        new_titles = ", ".join(f"'{x.title[:40]}'" for x in _new_windows(before)) or "none"
        raise Fail("TIMEOUT", f"no window for {name!r} after 10s (launched via {how}); new windows: {new_titles}", "list_windows()")
    ctx.opened.add(w.hwnd)
    front = await focus(w)
    _h().hint_focus(w.title)
    return ok(f"opened '{w.title}' (new window)", via=how, now="front" if front else f"behind '{(fg() or w).title[:40]}'")


@action("focus_window", group="WIN", summary="bring a window to the front and check it has the keyboard",
        params="window s part of the window title", cost=0.2, star=True, top="window", fallback='App(mode="switch", name)')
async def focus_window_(ctx: Ctx, window: str, **_) -> str:
    w = _need(ctx, window)
    if await focus(w):
        return ok(f"'{w.title}' is in front")
    popup = _u.GetWindow(w.hwnd, 6)  # GW_ENABLEDPOPUP: a dialog of its own holds the keyboard
    holder = fg()
    if popup and int(popup) != w.hwnd:
        raise Fail("NOT_FOCUSED", f"'{w.title[:40]}' has a dialog open: '{_text(popup)[:40]}'", "dismiss_dialog() or read_window(...)")
    raise Fail("NOT_FOCUSED", f"'{holder.title[:40] if holder else '?'}' keeps focus", "window_state(window, state=\"restore\")")


@action("window_state", group="WIN", summary="minimise, maximise, restore, snap left/right or move a window",
        params="""
        window s part of the window title
        state s min|max|restore|front|back|topmost|untopmost|left|right|monitor1|monitor2
        """, cost=0.2, star=True, top="window,state", fallback='App(mode="resize", ...)')
async def window_state(ctx: Ctx, window: str, state: str, **_) -> str:
    w = _need(ctx, window)
    guard_input(ctx, w)
    h = w.hwnd
    if state == "min":
        _u.ShowWindow(h, 6)
    elif state == "max":
        _u.ShowWindow(h, 3)
    elif state == "restore":
        _u.ShowWindow(h, 9)
    elif state == "front":
        await focus(w)
    elif state == "back":
        _u.SetWindowPos(h, 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # HWND_BOTTOM, no move/size/activate
    elif state in ("topmost", "untopmost"):
        _u.SetWindowPos(h, -1 if state == "topmost" else -2, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
    else:
        mons = _monitors()
        idx = (_monitor_of(_rect(h)) - 1) if state in ("left", "right") else int(state[-1]) - 1
        if not 0 <= idx < len(mons):
            raise Fail("BAD_ARGS", f"there are {len(mons)} monitors", f"window_state(window, state=\"monitor{len(mons)}\")")
        wl, wt_, wr, wb = mons[idx][1]
        zoomed = _u.IsZoomed(h)
        if zoomed or _u.IsIconic(h):
            _u.ShowWindow(h, 9)
            await asyncio.sleep(0.15)
        if state in ("left", "right"):
            half = (wr - wl) // 2
            x = wl if state == "left" else wl + half
            _u.SetWindowPos(h, 0, x, wt_, half, wb - wt_, 0x0004 | 0x0040)  # no z-order change, show
        else:
            l, t, r, b = _rect(h)
            cw, ch = min(r - l, wr - wl), min(b - t, wb - wt_)
            _u.SetWindowPos(h, 0, wl + 40, wt_ + 40, cw - 40 if cw > wr - wl - 40 else cw, ch - 40 if ch > wb - wt_ - 40 else ch, 0x0004 | 0x0040)
            if zoomed:
                await asyncio.sleep(0.1)
                _u.ShowWindow(h, 3)
    await asyncio.sleep(0.25)
    if not _u.IsWindow(h):
        raise Fail("NO_CHANGE", "the window closed")
    rect, iconic, zoomed = _rect(h), bool(_u.IsIconic(h)), bool(_u.IsZoomed(h))
    good = {"min": iconic, "max": zoomed, "restore": not iconic and not zoomed, "front": _is_front(h), "back": True,
            "topmost": bool(_u.GetWindowLongW(h, -20) & 0x8), "untopmost": not (_u.GetWindowLongW(h, -20) & 0x8)}.get(state)
    if good is None:  # snaps and moves: the rect landed where it should (±8 px)
        good = _monitor_of(rect) == (idx + 1) and (state not in ("left", "right") or abs(rect[0] - x) <= 8 or abs(rect[0] - (x - 7)) <= 8)
    desc = f"'{w.title[:50]}' is {'minimised' if iconic else 'maximised' if zoomed else 'normal'} at {rect} on monitor {_monitor_of(rect)}"
    if not good:
        if _elevated_hint(w):
            raise Fail("ELEVATED", f"'{w.title[:40]}' runs as administrator; IO can't move it", "ask_user")
        return unsure(f"asked for {state} but {desc}", "window_state(window, state=\"restore\") then try again")
    return ok(desc)


def _elevated_hint(w: W) -> bool:
    h = _k.OpenProcess(0x0400, False, w.pid)  # PROCESS_QUERY_INFORMATION is denied for elevated processes
    if h:
        _k.CloseHandle(h)
    return not h


@action("list_windows", group="WIN", summary="the open windows: title, app, front/min/max, monitor; what has focus",
        params="filter s? only windows whose title or app has this", cost=0.2, star=True, top="",
        fallback="Snapshot()", limits="titles only; read_window reads a window's text")
async def list_windows(ctx: Ctx, filter: str = "", **_) -> str:
    front = fg()
    lines = []
    for w in windows():
        if filter and filter.lower() not in (w.title + " " + w.exe).lower():
            continue
        if w.title in ("IO",) or w.pid == os.getpid():
            continue
        tags = [w.exe]
        if front and w.hwnd == front.hwnd:
            tags.append("front")
        if w.minimized:
            tags.append("min")
        elif w.maximized:
            tags.append("max")
        tags.append(f"monitor {_monitor_of(w.rect)}")
        if w.hwnd in ctx.opened:
            tags.append("IO opened")
        lines.append(f"'{w.title[:80]}' " + " ".join(f"[{t}]" for t in tags))
    if not lines:
        return ok("no windows match" if filter else "no app windows are open")
    tail = ""
    if front:
        try:
            info = await on_uia(_focused_info, timeout=2.0)
            tail = f"\nFocus: {front.exe} '{front.title[:50]}'" + (f" → {info}" if info else "")
        except Exception:
            tail = f"\nFocus: {front.exe} '{front.title[:50]}'"
        popup = _u.GetWindow(front.hwnd, 6)
        if popup and int(popup) != front.hwnd:
            tail += f"\nModal popup over it: '{_text(popup)[:60]}'"
    return ok(f"{len(lines)} windows:\n" + "\n".join(lines[:40]) + tail)


def _focused_info() -> str:
    from windows_mcp import uia
    c = uia.GetFocusedControl()
    if not c:
        return ""
    name = (c.Name or "").strip()[:50]
    return f'{c.ControlTypeName.removesuffix("Control")} "{name}"' + (" (password field)" if c.IsPassword else "")


SAVE_BUTTONS = {"discard": ["don't save", "dont save", "do not save", "no", "discard"], "save": ["save", "yes"],
                "cancel": ["cancel"]}


def _dialog_buttons_sync(hwnd: int) -> dict:
    """A save/confirm prompt inside or owned by the window: its text and buttons (Win32 #32770 or an in-window XAML dialog)."""
    out = {"text": "", "buttons": [], "hwnd": 0}
    popup = _u.GetWindow(hwnd, 6)
    for target in ([int(popup)] if popup and int(popup) != hwnd else []) + [hwnd]:
        try:
            nodes, _ = walk(target, 1500)
        except Exception:
            continue
        btns = [n for n in nodes if n.kind in ("Button", "SplitButton") and _norm(n.name) in
                {"save", "don't save", "dont save", "do not save", "cancel", "yes", "no", "ok", "close", "discard", "replace", "skip"}]
        names = {_norm(n.name) for n in btns}
        if btns and (names & {"don't save", "dont save", "do not save", "no", "discard", "replace"} or target != hwnd):
            texts = [n.name for n in nodes if n.kind == "Text" and len(n.name) > 12]
            out.update(text=" ".join(texts)[:200], buttons=[n.name for n in btns], hwnd=target)
            return out
    return out


def _press_button_sync(hwnd: int, names: list[str]) -> str:
    """Invokes the first button whose name is one of names in the window (or its dialog). Returns the button name."""
    popup = _u.GetWindow(hwnd, 6)
    for target in ([int(popup)] if popup and int(popup) != hwnd else []) + [hwnd]:
        try:
            nodes, elems = walk(target, 1500)
        except Exception:
            continue
        for want in names:
            for n in nodes:
                if n.kind in ("Button", "SplitButton") and _norm(n.name) == want and n.enabled is not False:
                    p = pattern(elems[n.i], "invoke")
                    if p:
                        p.Invoke(waitTime=0)
                        return n.name
    return ""


@action("close_window", group="WIN", summary="close a window like its X; handles the save prompt as told",
        params="""
        window s part of the window title
        unsaved s? ask|discard|save what to do if it asks to save
        """, cost=1.0, tier="uia", star=True, top="window,unsaved?", fallback="close_windows(titles)", timeout=20,
        risky=lambda a, c: _close_risky(a, c), limits="never kills a process; protected windows are refused")
async def close_window(ctx: Ctx, window: str, unsaved: str = "ask", **_) -> str:
    w = _need(ctx, window)
    guard_input(ctx, w)
    if _is_io(w):
        raise Fail("BLOCKED", "IO doesn't close itself")
    _u.PostMessageW(w.hwnd, 0x0010, 0, 0)  # WM_CLOSE, like the X button
    t_end = time.time() + 1.5
    while time.time() < t_end:
        await asyncio.sleep(0.15)
        if not _alive(w.hwnd):
            ctx.opened.discard(w.hwnd)
            # Win11 Notepad closes without asking and keeps unsaved tabs for its next start
            keep = " (Notepad keeps unsaved tabs for next time; close a tab with hotkeys([\"ctrl+w\"]) to discard it)" \
                if w.exe == "notepad.exe" and w.title.startswith("*") and unsaved == "discard" else ""
            return ok(f"closed '{w.title}'{keep}")
    try:
        prompt = await on_uia(_dialog_buttons_sync, w.hwnd, timeout=4.0)
    except UiaTimeout:
        prompt = {"buttons": []}
    if not prompt["buttons"]:
        if not _alive(w.hwnd):
            return ok(f"closed '{w.title}'")
        raise Fail("NO_CHANGE", f"'{w.title[:50]}' is still open and shows no save prompt IO can read", "read_window(...) or dismiss_dialog()")
    buttons = " / ".join(prompt["buttons"])
    if unsaved == "ask":
        return unsure(f"'{w.title[:50]}' asks \"{prompt['text'][:120]}\" — buttons: {buttons}",
                      f'close_window("{window}", unsaved="discard") or close_window("{window}", unsaved="save") or ask_user')
    if unsaved == "discard" and w.hwnd not in ctx.opened and not _named(ctx, *DISCARD_WORDS):
        # real unsaved work in a window IO didn't open, and the request never said to throw it away: the user decides
        answer = (await ctx.ask(f"'{w.title[:60]}' has unsaved changes. Close it and throw them away? (yes/no)")) if ctx.ask else "no"
        if not str(answer).strip().lower().startswith("y"):
            await on_uia(_press_button_sync, w.hwnd, ["cancel"], timeout=4.0)
            raise Fail("REFUSED", f"the user wants to keep the unsaved changes in '{w.title[:40]}'; it stays open",
                       f'save_file_as(path, window="{w.title[:30]}") or done')
    pressed = await on_uia(_press_button_sync, w.hwnd, SAVE_BUTTONS[unsaved], timeout=4.0)
    if not pressed:
        raise Fail("NOT_FOUND", f"no {unsaved} button in the prompt; it has {buttons}", "dismiss_dialog(choice=...)")
    t_end = time.time() + 3
    while time.time() < t_end:
        await asyncio.sleep(0.2)
        if not _alive(w.hwnd):
            ctx.opened.discard(w.hwnd)
            return ok(f"closed '{w.title}' (pressed \"{pressed}\")")
    if unsaved == "save":
        return unsure(f"pressed \"{pressed}\" but '{w.title[:40]}' is still open (a Save As dialog?)", "save_file_as(path) or list_windows()")
    raise Fail("NO_CHANGE", f"pressed \"{pressed}\" but '{w.title[:40]}' is still open", "read_window(...)")


def _close_risky(args: dict, ctx: Ctx) -> str:
    """Closing a window IO didn't open asks, unless the request names it. Throwing away unsaved work is asked about
    in close_window itself, when the window really shows a save prompt ("close paint" is not "lose my drawing", but
    Calculator has nothing to lose)."""
    w = resolve(ctx, str(args.get("window") or ""))
    if w is None or w.hwnd in ctx.opened or _named(ctx, *re.findall(r"[A-Za-z]{4,}", w.title)[:3]):
        return ""
    return f"close the window '{w.title[:60]}'" + (" without saving" if args.get("unsaved") == "discard" else "")


DISCARD_WORDS = ("without saving", "discard", "don't save", "dont save", "do not save", "throw away", "lose the changes")


def _keys_close_risky(args: dict, ctx: Ctx) -> str:
    """alt+f4 / ctrl+w by keys: closing a window (or a document tab, which in Win11 Notepad discards it) IO didn't open
    always asks, like close_window(unsaved="discard")."""
    w = resolve(ctx, str(args.get("window") or ""))
    if w is None or w.hwnd in ctx.opened:
        return ""
    combo = next((k for k in _keys_of(args) if CLOSE_KEYS.match(k)), "")
    return f"press {combo} in '{w.title[:60]}' (closes it, unsaved work included)"


BLOCKED_CHOICES = re.compile(r"\b(watch|ad|ads|buy|purchase|install|subscribe|upgrade|accept all|allow all|pay|claim with)\b", re.I)
DISMISS_WORDS = {"cancel": ["cancel", "close", "not now", "no thanks", "no", "skip", "later", "maybe later", "dismiss", "got it", "×", "x"],
                 "ok": ["ok", "okay", "got it", "continue"], "yes": ["yes"], "no": ["no", "don't save", "cancel"],
                 "close": ["close", "×", "x", "cancel"], "reject": ["reject all", "reject", "necessary only", "only necessary", "decline"]}
DESTROY_WORDS = re.compile(r"\b(delete|permanently|replace|overwrite|erase|format|discard|remove|lose)\b", re.I)
# a button that throws work away, whatever choice found it ("no" is "Don't save"; "close" prefix-matches "Close without saving")
DESTROY_BUTTON = re.compile(r"\b(delete|permanently|replace|overwrite|erase|format|discard|remove|lose)\b|don'?t save|do not save|without saving", re.I)


def _popup_of(w: W | None) -> W | None:
    """The dialog to dismiss: the window's own enabled popup, or the window itself if it is a dialog."""
    if w is None:
        return None
    p = _u.GetWindow(w.hwnd, 6)
    if p and int(p) != w.hwnd and _alive(int(p)):
        return _w(int(p))
    if w.cls == "#32770" or _u.GetWindow(w.hwnd, 4):
        return w
    return None


POPUP_ROOT = re.compile(r"popup|dialog|flyout|teachingtip|overlay", re.I)


def _ancestors(nodes: list[N], n: N):
    p = n.parent
    while p is not None and p >= 0:
        yield nodes[p]
        p = nodes[p].parent


def _popup_scope(nodes: list[N]) -> set | None:
    """Indexes of the nodes inside on-screen in-window popups (XAML Popup, ContentDialog, TeachingTip), or None.
    Win11 apps draw their dialogs inside the main window, next to its own title-bar Close button."""
    roots = {n.i for n in nodes if n.depth > 0 and n.kind in ("Window", "Pane", "Group") and POPUP_ROOT.search(f"{n.cls} {n.name}")
             and n.rect[2] - n.rect[0] > 20 and n.rect[3] - n.rect[1] > 20}
    if not roots:
        return None
    inside = {n.i for n in nodes if any(a.i in roots for a in _ancestors(nodes, n))}
    return inside or None


TIP_WORDS = ["close", "not now", "got it", "skip", "maybe later", "no thanks", "dismiss", "×", "x"]
DECISION_WORDS = {"save", "don't save", "dont save", "yes", "no", "ok", "delete", "replace", "retry", "allow", "accept", "install", "buy"}


def _tip_sync(hwnd: int) -> dict:
    """An in-window popup in front of the window's content: closes it when it is only informational (What's new, tips:
    a Close/Got it button and nothing to decide); otherwise says what it asks. {} when there is none."""
    nodes, elems = walk(hwnd, 1500)
    scope = _popup_scope(nodes)
    if not scope:
        return {}
    btns = [n for n in nodes if n.i in scope and n.kind in ("Button", "SplitButton", "Hyperlink") and n.enabled is not False]
    if not btns:
        return {}
    title = next((n.name for n in nodes if n.i in scope and n.kind == "Text" and n.name), "a popup")[:60]
    if any(_norm(n.name) in DECISION_WORDS for n in btns):
        return {"blocking": title, "buttons": [n.name for n in btns][:6]}
    for want in TIP_WORDS:
        for n in btns:
            if _norm(n.name) == want and "invoke" in n.pats:
                pattern(elems[n.i], "invoke").Invoke(waitTime=0)
                return {"closed": title, "button": n.name}
    return {"blocking": title, "buttons": [n.name for n in btns][:6]}


async def clear_tips(w: W, strict: bool = True) -> str:
    """Before keyboard input: '' if nothing was in the way, or a note that a tip popup was closed (Win11 Notepad shows
    "New in Notepad" over the text on every start, and it swallows typing). A popup that asks something is not closed
    here: with strict that is COVERED, for the decider to answer."""
    try:
        res = await on_uia(_tip_sync, w.hwnd, timeout=4.0)
    except UiaTimeout:
        return ""
    if res.get("closed"):
        await asyncio.sleep(0.3)
        return f" (closed the popup '{res['closed']}' first)"
    if res.get("blocking") and strict:
        raise Fail("COVERED", f"a popup '{res['blocking']}' is open over '{w.title[:40]}' with buttons {', '.join(res['buttons'])}",
                   f'dismiss_dialog(choice=...) or click("{res["buttons"][0]}")')
    return ""


def _dismiss_sync(hwnd: int, words: list[str], press: bool = True, only_popup: bool = False, allow_destroy: bool = False) -> dict:
    """Presses the first button matching words. A button that throws work away (Don't save, Delete, Close without saving)
    is pressed only with allow_destroy; otherwise it comes back as "destroy" for the user to decide."""
    nodes, elems = walk(hwnd, 1500)
    scope = _popup_scope(nodes)
    btns = [n for n in nodes if n.kind in ("Button", "SplitButton", "Hyperlink") and n.enabled is not False
            and not any(a.kind == "TitleBar" for a in _ancestors(nodes, n))]  # never the window's own Close
    if scope and any(n.i in scope for n in btns):
        btns = [n for n in btns if n.i in scope]
        text = " ".join(n.name for n in nodes if n.i in scope and n.kind == "Text" and len(n.name) > 8)[:300]
    elif only_popup:
        return {"done": "", "text": "", "buttons": [], "popup": False}
    else:
        text = " ".join(n.name for n in nodes if n.kind == "Text" and len(n.name) > 8)[:300]
    if not press:
        return {"done": "", "text": text, "buttons": [n.name for n in btns][:8]}
    destroy = ""
    for want in words:
        for n in btns:
            nm = _norm(n.name)
            if nm == want or (len(want) > 2 and nm.startswith(want)):
                if BLOCKED_CHOICES.search(n.name):
                    continue
                if DESTROY_BUTTON.search(n.name) and not allow_destroy:
                    destroy = destroy or n.name
                    continue
                p = pattern(elems[n.i], "invoke")
                if p:
                    p.Invoke(waitTime=0)
                    return {"done": n.name, "text": text, "how": "uia-invoke"}
                return {"done": "", "click": n.center, "name": n.name, "text": text}
    is_dialog = _cls(hwnd) == "#32770" or bool(_u.GetWindow(hwnd, 4))  # never WindowPattern.Close an app's main window
    if destroy:
        return {"done": "", "destroy": destroy, "text": text, "buttons": [n.name for n in btns][:8], "popup": bool(scope) or is_dialog}
    if nodes and "window" in nodes[0].pats and is_dialog and not scope:
        p = pattern(elems[0], "window")
        if p and words and words[0] in ("cancel", "close"):
            p.Close(waitTime=0)
            return {"done": "window close", "text": text, "how": "uia-window-close"}
    return {"done": "", "text": text, "buttons": [n.name for n in btns][:8], "popup": bool(scope) or is_dialog}


@action("dismiss_dialog", group="WIN", summary="close a popup or dialog by its Cancel/Close/OK button",
        params="""
        window s? the window the dialog belongs to
        choice s? cancel (default), ok, yes, no, close, reject
        """, cost=0.6, tier="uia", star=True, top="choice?", fallback="click_on(\"the X close button of the popup\")",
        limits="never presses buy/install/watch-ad/accept-all; Esc is never sent to emulators or games")
async def dismiss_dialog(ctx: Ctx, window: str = "", choice: str = "cancel", **_) -> str:
    if BLOCKED_CHOICES.search(choice or ""):
        raise Fail("BLOCKED", f"IO never presses {choice!r}", "dismiss_dialog(choice=\"cancel\")")
    base = resolve(ctx, window) if window else fg()
    target = _popup_of(base) or base
    if target is None:
        raise Fail("NOT_FOUND", "no dialog to dismiss", "list_windows()")
    guard_input(ctx, target)
    words = DISMISS_WORDS.get(choice.lower().strip(), [_norm(choice)])
    before = await snapshot(target)
    approved = False
    if choice.lower().strip() not in ("cancel", "close", "no", "reject"):
        # OK/Yes/Replace on a dialog that names destroying something: the user decides
        try:
            peek = await on_uia(_dismiss_sync, target.hwnd, [], False, timeout=4.0)
        except UiaTimeout:
            peek = {"text": ""}
        if DESTROY_WORDS.search(peek.get("text") or "") or DESTROY_WORDS.search(choice):
            answer = (await ctx.ask(f"The dialog says \"{peek.get('text', '')[:160]}\". Press {choice}? (yes/no)")) if ctx.ask else "no"
            if not str(answer).strip().lower().startswith("y"):
                raise Fail("REFUSED", f"the user did not allow pressing {choice} on '{target.title[:40]}'", "dismiss_dialog(choice=\"cancel\")")
            approved = True
    try:
        res = await on_uia(_dismiss_sync, target.hwnd, words, True, False, approved, timeout=4.0)
        if res.get("destroy"):  # the only match throws work away: the user decides, whatever choice found it
            answer = (await ctx.ask(f"The dialog's \"{res['destroy']}\" button would throw work away ({res.get('text', '')[:120]}). "
                                    f"Press it? (yes/no)")) if ctx.ask else "no"
            if not str(answer).strip().lower().startswith("y"):
                raise Fail("REFUSED", f"the user did not allow pressing \"{res['destroy']}\" on '{target.title[:40]}'",
                           "dismiss_dialog(choice=\"cancel\")")
            res = await on_uia(_dismiss_sync, target.hwnd, words, True, False, True, timeout=4.0)
    except UiaTimeout:
        res = {"done": "", "text": ""}
    if res.get("click"):
        await _uncovered(target, res["click"])
        await asyncio.to_thread(mouse_click, *res["click"])
        res["done"], res["how"] = res["name"], "click"
    if not res.get("done") and choice.lower() in ("cancel", "close") and target.exe not in EMULATORS and not ctx.loop and res.get("popup"):
        if await focus(target):
            press(target, "esc")
            res["done"], res["how"] = "Esc", "key"
    if not res.get("done"):
        try:
            got = await asyncio.to_thread(eyes(ctx).find, "the X or Close button of the popup or dialog (not an ad, not a purchase)", 0, target.title)
        except Exception as e:
            got = {"error": str(e)}
        if "x" in got:
            pt = (int(got["x"]), int(got["y"]))
            loop_guard(ctx, pt)
            await _uncovered(target, pt)
            await asyncio.to_thread(mouse_click, *pt)
            res["done"], res["how"] = "the X it saw", "vision"
            note_point(ctx, pt)
    if not res.get("done"):
        raise Fail("NOT_FOUND", f"no {choice} button in '{target.title[:40]}'; buttons: {', '.join(res.get('buttons') or []) or 'none'}",
                   "click(\"<button name>\") or ask_user")
    what = await wait_change(target, before, limit=1.5)
    if not what:
        return unsure(f"pressed \"{res['done']}\" in '{target.title[:40]}' but nothing changed", "check_screen(\"is the dialog gone?\")")
    return ok(f"pressed \"{res['done']}\" in '{target.title[:40]}' ({'it closed' if what == 'window gone' else what + ' changed'})",
              via=res.get("how", ""))


# ======================================================================================================================
# L2 · READ: reading without side effects
# ======================================================================================================================

_LINE_KIND = {"Button": "button", "SplitButton": "button", "MenuItem": "menu", "TabItem": "tab", "Hyperlink": "link", "CheckBox": "checkbox",
              "RadioButton": "radio", "ComboBox": "dropdown", "Slider": "slider", "Image": "", "HeaderItem": "column"}


def _read_sync(hwnd: int, title: str) -> dict:
    """The window's text in reading order: names, field and document values (TextPattern), list items, status bar."""
    nodes, elems = walk(hwnd, 3000)
    lines, named, docs = [], 0, []
    last = ""
    for n in nodes:
        if n.name:
            named += 1
        line = ""
        if n.kind in ("Document", "Edit") and ("text" in n.pats or n.value):
            body = n.value
            if "text" in n.pats:
                try:
                    p = pattern(elems[n.i], "text")
                    body = p.DocumentRange.GetText(20000) if p else body
                except Exception:
                    pass
            body = "•••" if n.password else (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
            label = n.name if n.name and n.name != body else ""
            if n.kind == "Edit" and "\n" not in body:
                line = f"[field {label}] = {body}" if label else f"[field] = {body}"
            else:
                # the document's own text goes first, fenced: read amid tab names and buttons, small models answered
                # with the file name ("otter.txt") instead of the text ("BENCH-OTTER-77")
                docs.append((label, body))
                continue
        elif n.name:
            if n.name == title and n.depth <= 1:
                continue
            k = _LINE_KIND.get(n.kind)
            if k is None:
                line = ("- " if n.kind in ("ListItem", "TreeItem", "DataItem") else "") + n.name
            elif k:
                st = n.state()
                line = f"[{k}] {n.name}" + (f" ({st})" if st else "")
            if n.value and n.kind in ("ComboBox", "Slider", "Spinner") and n.value != n.name:
                line += f" = {n.value}"
        if line and line != last:
            lines.append(line)
            last = line
    seen, out = set(), []
    for l in lines:  # the same label repeated by nested elements reads once
        if len(l) < 80 and l in seen:
            continue
        seen.add(l)
        out.append(l)
    has_doc = any(n.kind in ("Document", "Edit") and ("text" in n.pats or n.value) for n in nodes)
    head = []
    for label, body in docs:
        head.append(f"Document text{' (' + label + ')' if label else ''}, {len(body)} characters:\n\"\"\"\n{body}\n\"\"\"" if body
                    else f"Document text{' (' + label + ')' if label else ''}: (empty)")
    if head and out:
        head.append("The rest of the window (tabs, menus, buttons):")
    return {"text": "\n".join(head + out), "named": named, "doc": has_doc, "nodes": len(nodes)}


@action("read_window", group="READ", summary="the text of one window (fields, document, labels, display) as text",
        params="""
        window s part of the window title
        find s? only the lines about these words
        max i? most characters to return (3000)
        """, cost=0.2, tier="uia", star=True, top="window,find?", fallback="Snapshot()", hide=("max",),
        limits="realised list rows only; no colours or images; games/canvases: read_region")
async def read_window(ctx: Ctx, window: str = "", find: str = "", max: int = 3000, **_) -> str:
    w = _need(ctx, window)
    guard_read(ctx, w)
    res = await on_uia(_read_sync, w.hwnd, w.title, timeout=5.0)
    if res["named"] < 5 and not res["doc"]:
        raise Fail("UNSUPPORTED", f"'{w.title[:50]}' exposes no text (canvas/game)", f'read_region("{window or w.title[:30]}")')
    text, budget = res["text"], builtins_max(300, min(int(max or 3000), 12000))
    # find= only narrows a long window: a short one comes whole (find="document" matched only the "Document text"
    # header and hid the text itself)
    if find and len(text) > 1500 and _norm(find) not in DOC_FIELDS + ("content", "contents", "all", "everything", "window"):
        text = _h().find_in_text(text, find, budget=budget)
    elif len(text) > budget:
        text = text[:budget] + f"\n[{len(res['text']) - budget} more characters; use find= to look for something]"
    return ok(f"'{w.title}' says:\n{text}")


def _controls_sync(hwnd: int, kinds: set | None, flt: str) -> list[tuple]:
    nodes, _ = walk(hwnd, 3000)
    wrect = _rect(hwnd)
    out = []
    for n in nodes:
        if n.kind not in ACTIONABLE or n.kind == "MenuBar" or not n.visible_in(wrect):
            continue
        if kinds and n.kind not in kinds:
            continue
        label = n.name or n.help or n.aid
        if not label and n.kind not in ("Edit", "Document", "ComboBox"):
            continue
        if flt and flt.lower() not in (label or "").lower():
            continue
        out.append((n.kind, label or "", n.state(), n.center, n.rect, n.aid))
    return out


_listed: dict = {}  # hwnd -> (time, [(kind, name, state, center, rect, aid)]): list_controls' #n stay valid for 2 minutes


@action("list_controls", group="READ", summary="numbered clickable controls in a window: kind, name, state, (x,y)",
        params="""
        window s part of the window title
        kind s? button, field, menu, tab, item, checkbox, link...
        filter s? only names containing this
        """, cost=0.2, tier="uia", star=True, top="window,kind?", fallback="Snapshot()", hide=("filter",),
        limits="on-screen controls only; click(\"#n\") uses the last list's numbers")
async def list_controls(ctx: Ctx, window: str = "", kind: str = "any", filter: str = "", **_) -> str:
    w = _need(ctx, window)
    guard_read(ctx, w)
    found = await on_uia(_controls_sync, w.hwnd, _kinds(kind), filter, timeout=5.0)
    if not found:
        if not filter and kind in ("", "any"):
            raise Fail("UNSUPPORTED", f"'{w.title[:50]}' exposes no controls (canvas/game)", f'click("<what it looks like>", window="{w.title[:30]}", how="vision")')
        raise Fail("NOT_FOUND", f"no {kind if kind != 'any' else ''} controls{' with ' + repr(filter) if filter else ''} in '{w.title[:40]}'",
                   f'list_controls("{window}")')
    _listed[w.hwnd] = (time.time(), found)
    lines = [f'#{i} {k.lower()} "{nm[:60]}"' + (f" [{st}]" if st else "") + f" ({c[0]},{c[1]})" for i, (k, nm, st, c, _, _) in enumerate(found[:60], 1)]
    more = f"\n(+{len(found) - 60} more; use filter=)" if len(found) > 60 else ""
    return ok(f"'{w.title[:50]}': {len(found)} controls\n" + "\n".join(lines) + more)


def _find_sync(hwnd: int, text: str, kinds: set | None) -> dict:
    nodes, _ = walk(hwnd, 3000)
    wrect = _rect(hwnd)
    hits, tier = candidates(nodes, text, kinds, wrect, fuzzy=True)
    out = []
    for n in hits[:5]:
        d = {"kind": n.kind, "name": n.name, "value": ("•••" if n.password else n.value[:200]) if n.value else "", "state": n.state(),
             "enabled": n.enabled is not False, "on_screen": n.visible_in(wrect), "rect": n.rect, "center": n.center, "aid": n.aid}
        if n.kind == "Text" and (field := label_target(nodes, n, {"Edit", "ComboBox", "Document", "Spinner"})):
            d["labels"] = f'{field.kind} value="{"•••" if field.password else (field.value or "")[:120]}"'
        out.append(d)
    return {"hits": out, "tier": tier, "closest": "" if out else closest_names(nodes, text, kinds)}


def _describe(d: dict) -> str:
    bits = [d["kind"].lower(), f'"{d["name"][:60]}"']
    if d.get("value"):
        bits.append(f'value="{d["value"][:80]}"')
    if d.get("state"):
        bits.append(f"[{d['state']}]")
    if not d["enabled"]:
        bits.append("disabled")
    bits.append(f"at ({d['center'][0]},{d['center'][1]})" if d["on_screen"] else "(off screen)")
    if d.get("labels"):
        bits.append(f"labels {d['labels']}")
    return " ".join(bits)


@action("find_control", group="READ", summary="is a control there? its kind, value, state and whether it's enabled",
        params="""
        window s part of the window title
        text s the control's name, label or placeholder
        kind s? button, field, checkbox, ...
        nth i? which match, 1 = first
        """, cost=0.2, tier="uia", star=True, top="window,text", fallback="Snapshot()")
async def find_control(ctx: Ctx, text: str, window: str = "", kind: str = "any", nth: int = 0, **_) -> str:
    w = _need(ctx, window)
    guard_read(ctx, w)
    res = await on_uia(_find_sync, w.hwnd, text, _kinds(kind), timeout=5.0)
    hits = res["hits"]
    if not hits:
        raise Fail("NOT_FOUND", f'no control "{text}" in \'{w.title[:40]}\'' + (f"; closest: {res['closest']}" if res["closest"] else ""),
                   f'list_controls("{window or w.title[:30]}") or scroll_until("{text}")')
    if res["tier"] == 5:
        return ok(f'no exact "{text}", but a look-alike: ' + "; ".join(_describe(d) for d in hits[:3]))
    if nth:
        if not 1 <= nth <= len(hits):
            raise Fail("BAD_ARGS", f"only {len(hits)} matches", f'find_control("{window}", "{text}", nth=1)')
        return ok("yes: " + _describe(hits[nth - 1]))
    if len(hits) == 1:
        return ok("yes: " + _describe(hits[0]))
    return ok(f"yes, {len(hits)} matches: " + "; ".join(f"#{i} {_describe(d)}" for i, d in enumerate(hits, 1)))


def _table_sync(hwnd: int, name: str, max_rows: int) -> dict:
    nodes, elems = walk(hwnd, 5000, max_depth=30)
    tables = [n for n in nodes if n.kind in ("DataGrid", "List", "Table", "Tree")]
    if name:
        tables = candidates(tables, name)[0] or [t for t in tables if name.lower() in (t.aid or "").lower()]
    if not tables:
        return {"error": "no list or table" + (f" named {name!r}" if name else "")}
    children: dict = {}
    for n in nodes:
        children.setdefault(n.parent, []).append(n)

    def rows_of(t):
        return [c for c in children.get(t.i, []) if c.kind in ("DataItem", "ListItem", "TreeItem")]

    t = max(tables, key=lambda t: len(rows_of(t)))
    header = []
    for c in children.get(t.i, []):
        if c.kind == "Header":
            header = [h.name for h in children.get(c.i, []) if h.kind == "HeaderItem" and h.name]
    rows = []
    for r in rows_of(t)[:max_rows]:
        cells = [c.value or c.name for c in children.get(r.i, []) if c.kind in ("Text", "Edit", "DataItem", "Custom") and (c.value or c.name)]
        if r.name and (not cells or cells[0] != r.name):
            cells = [r.name] + [x for x in cells if x != r.name]
        rows.append(cells or [r.name])
    total = len(rows_of(t))
    from windows_mcp import uia
    try:  # virtualised lists only realise visible rows; GridPattern knows the real count
        g = control(elems[t.i]).GetPattern(uia.PatternId.GridPattern)
        total = max(total, int(g.RowCount)) if g else total
    except Exception:
        pass
    return {"name": t.name or t.kind, "header": header, "rows": rows, "total": total}


@action("read_table", group="READ", summary="a list view or grid as tab-separated rows with headers",
        params="""
        window s part of the window title
        name s? which list or table, if several
        max_rows i? most rows (50)
        """, cost=0.6, tier="uia", star=True, top="window", fallback="Snapshot()",
        limits="realised rows only (scroll for more); for folders list_files is better")
async def read_table(ctx: Ctx, window: str = "", name: str = "", max_rows: int = 50, **_) -> str:
    w = _need(ctx, window)
    guard_read(ctx, w)
    res = await on_uia(_table_sync, w.hwnd, name, max(1, min(int(max_rows or 50), 500)), timeout=6.0)
    if "error" in res:
        raise Fail("NOT_FOUND", f"{res['error']} in '{w.title[:40]}'", f'read_window("{window or w.title[:30]}")')
    lines = (["\t".join(res["header"])] if res["header"] else []) + ["\t".join(r) for r in res["rows"]]
    more = f"\n({res['total'] - len(res['rows'])} more rows; scroll_until or max_rows=)" if res["total"] > len(res["rows"]) else ""
    return ok(f"'{res['name'][:40]}' in '{w.title[:40]}', {len(res['rows'])} of {res['total']} rows:\n" + "\n".join(lines) + more)


AREAS = {"whole": (0, 0, 1, 1), "top": (0, 0, 1, 0.5), "bottom": (0, 0.5, 1, 1), "left": (0, 0, 0.5, 1), "right": (0.5, 0, 1, 1)}


@action("read_region", group="READ", summary="transcribe the text in part of a window with vision (games, images)",
        params="""
        window s part of the window title
        area s? whole, top, bottom, left, right or x0,y0,x1,y1 (0-1)
        numbers b? true: read twice, mark digits that differ
        """, cost=4.0, tier="vision", top="window,area?", fallback="look_at_screen(question, window)",
        limits="small models misread digits; no coordinates; only after read_window is UNSUPPORTED")
async def read_region(ctx: Ctx, window: str = "", area: str = "whole", numbers: bool = False, **_) -> str:
    w = _need(ctx, window)
    guard_read(ctx, w)
    frac = AREAS.get((area or "whole").lower())
    if frac is None:
        try:
            frac = tuple(float(v) for v in re.findall(r"[\d.]+", area)[:4])
            assert len(frac) == 4 and 0 <= frac[0] < frac[2] <= 1 and 0 <= frac[1] < frac[3] <= 1
        except (ValueError, AssertionError):
            raise Fail("BAD_ARGS", "area is whole|top|bottom|left|right or x0,y0,x1,y1 as 0-1 fractions", 'read_region(window, area="top")')
    await focus(w)
    await asyncio.sleep(0.3)
    l, t, r, b = _area_rect(ctx, w)
    box = (int(l + frac[0] * (r - l)), int(t + frac[1] * (b - t)), int(l + frac[2] * (r - l)), int(t + frac[3] * (b - t)))
    img = await asyncio.to_thread(grab, box)
    if img.width < 800:
        img = img.resize((img.width * 2, img.height * 2))
    prompt = "Transcribe exactly the text in this image. Output only the text."
    first = await asyncio.to_thread(vlm, img, prompt, 400)
    if numbers:
        second = await asyncio.to_thread(vlm, img, prompt, 400)
        a, b2 = first.splitlines(), second.splitlines()
        first = "\n".join(x if x == (b2[i] if i < len(b2) else None) else f"{x} ?" for i, x in enumerate(a))
    if not first.strip():
        return unsure(f"no text read in the {area} of '{w.title[:40]}'", "check_screen(...) or look_at_screen(...)")
    return ok(f"text in the {area} of '{w.title[:40]}' (vision; digits can be misread):\n{first[:3000]}")


@action("check_screen", group="READ", summary="answer a yes/no question about what a window shows (vision)",
        params="""
        question s a yes/no question
        window s? part of the window title (else the main display)
        """, cost=3.0, tier="vision", star=True, top="question", fallback="look_at_screen(question)",
        limits="vision: can be wrong; in normal apps find_control is exact")
async def check_screen(ctx: Ctx, question: str, window: str = "", **_) -> str:
    w = resolve(ctx, window) if (window or ctx.focus) else None
    if window and w is None:
        raise Fail("NOT_FOUND", f"no window matching {window!r}", "list_windows()")
    if w is not None:
        guard_read(ctx, w)
    ans, why = await yes_no(ctx, question, w)
    if not ans:
        return unsure(f"the answer was neither yes nor no: {why}", "look_at_screen(question)")
    return ok(f"{ans} — {why}")


# ======================================================================================================================
# L2 · ACT: acting, with checks
# ======================================================================================================================

XY = re.compile(r"^\s*[\[(]?\s*(-?\d{1,5})\s*[, ]\s*(-?\d{1,5})\s*[\])]?\s*$")
CRED = re.compile(r"password|passcode|passphrase|\bpin\b|\bcvv\b|\bcvc\b|card number|credit card|security code|\bssn\b|social security|"
                  r"\biban\b|routing number|account number|one-time code|\b2fa\b|\botp\b", re.I)


def _card_like(text: str) -> bool:
    """13-19 digits that pass the Luhn check: a payment card number, never typed by IO."""
    digits = re.sub(r"[ -]", "", text or "")
    if not re.fullmatch(r"\d{13,19}", digits):
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        v = int(d) * (2 if i % 2 else 1)
        total += v - 9 if v > 9 else v
    return total % 10 == 0


KEY_NAMES = {**{d: [w] for d, w in zip("0123456789", ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"))},
             "*": ["multiply by", "multiply"], "x": ["multiply by"], "×": ["multiply by"], "+": ["plus", "add"], "-": ["minus", "subtract"],
             "−": ["minus"], "/": ["divide by", "divide"], "÷": ["divide by"], "=": ["equals", "equal"], ".": ["decimal separator", "point"],
             ",": ["decimal separator"], "%": ["percent"], "c": ["clear"], "ce": ["clear entry"], "+/-": ["positive negative"],
             "±": ["positive negative"], "back": ["backspace"], "⌫": ["backspace"], "(": ["left parenthesis"], ")": ["right parenthesis"]}


def _uia_named(nodes: list[N]) -> int:
    return sum(1 for n in nodes if n.name)


def _click_sync(hwnd: int, target: str, kinds: set | None, nth: int, button: str, double: bool, listed: tuple | None,
                progress: dict) -> dict:
    """COM thread: find the control and act through its pattern when that is what a click means here."""
    nodes, elems = walk(hwnd, 3000)
    wrect = _rect(hwnd)
    named = _uia_named(nodes)
    if listed:  # "#n" from list_controls: the control at that rect (or the nearest with that name)
        kind, name, _st, center, rect, aid = listed
        same = [n for n in nodes if n.kind == kind and (n.name or n.help or n.aid) == name]
        if not same:
            return {"status": "real", "center": center, "name": name, "kind": kind, "named": named}
        hits = [min(same, key=lambda n: abs(n.center[0] - center[0]) + abs(n.center[1] - center[1]))]
    else:
        hits, _tier = candidates(nodes, target, kinds, wrect)
        if not hits and _norm(target) in KEY_NAMES:  # "7", "*", "=": Calculator and keypads name their buttons in words
            for alias in KEY_NAMES[_norm(target)]:
                hits, _tier = candidates(nodes, alias, kinds, wrect)
                if hits:
                    break
    if not hits:
        return {"status": "not_found", "named": named, "closest": closest_names(nodes, target, kinds)}
    if len(hits) > 1 and not nth:
        return {"status": "ambiguous", "list": [(n.kind, n.name, n.center) for n in hits[:5]], "n": len(hits), "named": named}
    if nth and not 1 <= nth <= len(hits):
        return {"status": "bad_nth", "n": len(hits), "named": named}
    n = hits[nth - 1] if nth else hits[0]
    if n.enabled is False:
        return {"status": "disabled", "name": n.name, "kind": n.kind, "named": named}
    e = elems[n.i]
    center = n.center
    if not n.visible_in(wrect) and "scrollitem" in n.pats:
        try:
            pattern(e, "scrollitem").ScrollIntoView(waitTime=0)
            time.sleep(0.15)
            r = live_rect(e)
            center = ((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
        except Exception:
            pass
    out = {"status": "real", "center": center, "name": n.name or n.aid, "kind": n.kind, "named": named, "rect": n.rect}
    if button != "left" or double:
        return out
    p = n.pats
    try:
        progress["acting"] = n.name  # an Invoke that opens a modal dialog can block this thread: click() reads this on timeout
        if n.kind in ("Button", "SplitButton", "MenuItem", "Hyperlink") and "invoke" in p and "toggle" not in p:
            pattern(e, "invoke").Invoke(waitTime=0)
            out.update(status="acted", via="uia-invoke")
        elif "toggle" in p and n.kind in ("CheckBox", "Button", "ListItem", "MenuItem"):
            tp = pattern(e, "toggle")
            before = tp.ToggleState
            tp.Toggle(waitTime=0)
            time.sleep(0.12)
            after = tp.ToggleState
            out.update(status="acted", via="uia-toggle", state={0: "off", 1: "on", 2: "mixed"}.get(after, str(after)), confirmed=after != before)
        elif "select" in p and n.kind in ("TabItem", "ListItem", "RadioButton", "DataItem", "TreeItem"):
            sp = pattern(e, "select")
            sp.Select(waitTime=0)
            time.sleep(0.12)
            out.update(status="acted", via="uia-select", state="selected" if sp.IsSelected else "not selected", confirmed=bool(sp.IsSelected))
        elif "expand" in p and n.kind in ("ComboBox", "TreeItem", "MenuItem", "SplitButton"):
            ep = pattern(e, "expand")
            if ep.ExpandCollapseState == 1:
                ep.Collapse(waitTime=0)
            else:
                ep.Expand(waitTime=0)
            time.sleep(0.12)
            out.update(status="acted", via="uia-expand", state={0: "collapsed", 1: "expanded"}.get(ep.ExpandCollapseState, ""))
        elif "invoke" in p:
            pattern(e, "invoke").Invoke(waitTime=0)
            out.update(status="acted", via="uia-invoke")
    except Exception:
        out["status"] = "real"  # the pattern refused: a real click at its centre
    progress.pop("acting", None)
    return out


def _name_at_sync(x: int, y: int) -> tuple[str, str]:
    from windows_mcp import uia
    c = uia.ControlFromPoint(int(x), int(y))
    return ((c.Name or "").strip(), c.ControlTypeName.removesuffix("Control")) if c else ("", "")


def _ground_on(ctx: Ctx, img, target: str) -> tuple[float, float] | None:
    """The eyes model on an image IO cut itself (the zoomed crop of careful=true): a point as fractions, or None."""
    h, e = _h(), eyes(ctx)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return h.evo_point(e.client, "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(), target)


async def vision_point(ctx: Ctx, w: W | None, target: str, careful: bool = False) -> tuple[tuple[int, int], str]:
    """(point, rung): eyes.find, then the UIA cross-check and the zoomed re-ground when careful."""
    e = eyes(ctx)
    title = (ctx.focus if ctx.loop and ctx.focus else w.title) if w else ""
    got = await asyncio.to_thread(e.find, target, 0, title)
    if "x" not in got:
        raise Fail("NOT_FOUND", f"vision could not locate {target!r}" + (f" in '{title[:40]}'" if title else "") + f" ({got.get('error', '')[:80]})",
                   f'read_window("{title[:30]}") or check_screen("is {target[:40]} visible?")')
    pt, rung = (int(got["x"]), int(got["y"])), "vision"
    textlike = target.strip()[:1] in "\"'" or len(target.split()) <= 3
    if textlike and not careful:
        # the eyes can point at a near miss; where the window has a UIA tree, the name at that point says whether it is
        # plausibly the target (a mismatch means a closer, zoomed look)
        try:
            name, _k2 = await on_uia(_name_at_sync, *pt, timeout=1.5)
        except Exception:
            name = ""
        words = set(re.findall(r"\w{3,}", target.lower()))
        if name and words and not words & set(re.findall(r"\w{3,}", name.lower())):
            careful = True
    if careful:
        l, t, r, b = (pt[0] - 320, pt[1] - 320, pt[0] + 320, pt[1] + 320)
        img = await asyncio.to_thread(grab, (l, t, r, b))
        img = img.resize((img.width * 2, img.height * 2))
        frac = await asyncio.to_thread(_ground_on, ctx, img, target)
        if frac is None:
            raise Fail("NOT_FOUND", f"vision says {target!r} isn't there on a closer look", "check_screen(...) or another description")
        else:
            new = (int(l + frac[0] * 640), int(t + frac[1] * 640))
            if abs(new[0] - pt[0]) + abs(new[1] - pt[1]) > 80:
                pt = new
            rung = "vision-careful"
    return pt, rung


async def _click_xy(ctx: Ctx, w: W | None, pt, button: str, double: bool) -> str:
    loop_guard(ctx, pt)
    if w is not None:
        await _uncovered(w, pt)
    if button == "left" and not double:
        return await asyncio.to_thread(_h().quick_click, int(pt[0]), int(pt[1]))
    return await asyncio.to_thread(mouse_click, int(pt[0]), int(pt[1]), button, 2 if double else 1)


@action("click", group="ACT", summary="click a control by text, #n, look or x,y; checks it changed",
        params="""
        target s text, "#n", a look, or "x,y"
        window s? part of the window title
        button s? left|right|middle
        double b? true for a double click
        nth i? which match when several (1 = first)
        how s? auto|uia|vision
        careful b? vision: re-check on a zoomed crop
        expect s? what should show afterwards
        """, cost=0.3, tier="uia", star=True, expect=True, top="target,window?", fallback="click_on(description)", hide=("double", "careful", "how"),
        limits="the vision rung can miss; elevated windows can't be clicked")
async def click(ctx: Ctx, target: str, window: str = "", button: str = "left", double: bool = False, nth: int = 0, how: str = "auto",
                careful: bool = False, **_) -> str:
    m = XY.match(target)
    w = resolve(ctx, window) if (window or not m) else (resolve(ctx, "") if ctx.focus else None)
    if m and w is None:  # a bare point: the window under it
        under = _u.WindowFromPoint(wt.POINT(int(m.group(1)), int(m.group(2))))
        w = _w(_u.GetAncestor(under, 2)) if under else None
    if w is None:
        raise Fail("NOT_FOUND", f"no window matching {window!r}" if window else "no window to click in", "list_windows()")
    guard_input(ctx, w)
    if ctx.loop and ctx.focus:
        how = "vision" if how == "auto" and not m else how
    if not await focus(w) and not (ctx.loop and ctx.focus):
        pass  # patterns work without focus; real clicks check what covers the point
    before_wins = {x.hwnd for x in windows(owned=True)}
    if m:
        pt = (int(m.group(1)), int(m.group(2)))
        before = await snapshot(w)
        done = await _click_xy(ctx, w, pt, button, double)
        what = await wait_change(w, before, new_from=before_wins)
        if not what:
            return unsure(f"clicked ({pt[0]}, {pt[1]}) but '{w.title[:40]}' looks the same", "check_screen(\"did it work?\") or another target")
        return ok(f"clicked ({pt[0]}, {pt[1]}) in '{w.title[:40]}' ({what} changed)", via="xy")
    listed = None
    if re.fullmatch(r"#\d+", target.strip()):
        stamp, items = _listed.get(w.hwnd, (0, []))
        k = int(target.strip()[1:])
        # an older list still names its controls (found again by kind and name below); with none at all, the numbers
        # list_controls(window) would give now (same order)
        if time.time() - stamp > 120 or not items:
            items = await on_uia(_controls_sync, w.hwnd, None, "", timeout=4.0)
            _listed[w.hwnd] = (time.time(), items)
        if not 1 <= k <= len(items):
            raise Fail("BAD_ARGS", f"{target} isn't a current list_controls number for '{w.title[:40]}'", f'list_controls("{w.title[:30]}")')
        listed = items[k - 1]
    use_uia = how in ("auto", "uia") and (how == "uia" or (w.exe not in EMULATORS and rung_ok(w.exe, "click", "uia")))
    if use_uia:
        before = await snapshot(w)
        progress: dict = {}
        try:
            res = await on_uia(_click_sync, w.hwnd, target, None, nth, button, double, listed, progress, timeout=4.0)
        except UiaTimeout:
            if progress.get("acting"):
                # the click went in; its handler is still running (usually a modal dialog it opened)
                new = _new_windows(before_wins)
                return ok(f"invoked \"{progress['acting']}\"" + (f"; '{new[0].title[:50]}' opened" if new else "; the app is busy"), via="uia-invoke")
            raise
        st = res["status"]
        if st == "not_found":
            if res["named"] < 5:
                rung_result(w.exe, "click", "uia", False)
            if how == "uia":
                if res["named"] < 5:
                    raise Fail("UNSUPPORTED", f"'{w.title[:40]}' has no UI Automation tree", f'click("{target}", how="vision")')
                raise Fail("NOT_FOUND", f'no control "{target}" in \'{w.title[:40]}\'' + (f"; closest: {res['closest']}" if res.get("closest") else ""),
                           f'click("{target}", how="vision") or list_controls("{w.title[:30]}")')
            closest = res.get("closest", "")
            if res["named"] >= 5 and closest and target.strip()[:1] not in "\"'" and len(target.split()) <= 3:
                # a text target with near names: say so instead of letting vision guess on a window that has a tree
                if _norm(target) and any(_norm(target)[:4] in _norm(c) for c in closest.split(", ")):
                    raise Fail("NOT_FOUND", f'no control "{target}" in \'{w.title[:40]}\'; closest: {closest}',
                               f'click({closest.split(", ")[0]}) or click("{target}", how="vision")')
        elif st == "ambiguous":
            opts = " ".join(f"#{i} {k.lower()} \"{n[:30]}\" ({c[0]},{c[1]})" for i, (k, n, c) in enumerate(res["list"], 1))
            raise Fail("AMBIGUOUS", f"{res['n']} controls match \"{target}\": {opts}", f'click("{target}", nth=1)')
        elif st == "bad_nth":
            raise Fail("BAD_ARGS", f"only {res['n']} matches for \"{target}\"", f'click("{target}", nth=1)')
        elif st == "disabled":
            raise Fail("DISABLED", f"{res['kind'].lower()} \"{res['name']}\" is disabled", "fill in the required fields first, or find_control(...)")
        elif st in ("acted", "real"):
            rung_result(w.exe, "click", "uia", True)
            label = f"{res['kind'].lower()} \"{res['name'][:50]}\""
            if st == "acted" and res.get("confirmed"):
                return ok(f"clicked {label} in '{w.title[:40]}': now {res.get('state')}", via=res["via"])
            if st == "acted":
                what = await wait_change(w, before, new_from=before_wins)
                if what:
                    return ok(f"clicked {label} in '{w.title[:40]}' ({what}{' changed' if what in ('tree', 'screen') else ''})", via=res["via"])
                # some providers report success and do nothing: once more as a real click
            await _click_xy(ctx, w, res["center"], button, double)
            what = await wait_change(w, before, new_from=before_wins)
            if what:
                return ok(f"clicked {label} in '{w.title[:40]}' ({what}{' changed' if what in ('tree', 'screen') else ''})", via="uia-click")
            return unsure(f"clicked {label} at {res['center']} but '{w.title[:40]}' looks the same",
                          f'find_control("{w.title[:30]}", "{target}") or check_screen("did it work?")')
    if how == "uia":
        raise Fail("UNSUPPORTED", f"'{w.title[:40]}' has no UI Automation tree", f'click("{target}", how="vision")')
    # vision rung
    pt, rung = await vision_point(ctx, w, target, careful)
    if ctx.loop and ctx.focus:
        area = _h().content_rect(ctx.focus)
        if area and not (area[0] <= pt[0] < area[2] and area[1] <= pt[1] < area[3]):
            raise Fail("NOT_FOUND", f"vision placed {target!r} outside the {ctx.focus} window", "check_screen(...) or another description")
    elif w.exe not in EMULATORS and target.strip()[:1] not in "\"'" and len(target.split()) <= 3:
        try:  # vision pointed at a named control that isn't the target: don't click it
            name, kind = await on_uia(_name_at_sync, *pt, timeout=1.5)
        except Exception:
            name, kind = "", ""
        words = set(re.findall(r"\w{3,}", target.lower()))
        if name and kind in ACTIONABLE and words and not words & set(re.findall(r"\w{3,}", name.lower())):
            raise Fail("NOT_FOUND", f"vision pointed at {kind.lower()} \"{name[:40]}\", not {target!r}", f'list_controls("{w.title[:30]}") or click("{name[:30]}")')
    before = (await tree_sig(w.hwnd) if w.exe not in EMULATORS else "", await sig_of(_area_rect(ctx, w)))
    await _click_xy(ctx, w, pt, button, double)
    note = note_point(ctx, pt)
    what = await wait_change(w, before, new_from=before_wins)
    if not what:
        return unsure(f"clicked ({pt[0]}, {pt[1]}) \"{target[:40]}\" via {rung} but the window looks the same" + note,
                      f'check_screen("did {target[:30]} work?") or click("{target[:40]}", careful=true)')
    return ok(f"clicked \"{target[:50]}\" at ({pt[0]}, {pt[1]}) ({what}{' changed' if what in ('tree', 'screen') else ''})" + note, via=rung)


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


DOC_FIELDS = ("", "document", "editor", "main", "text", "body", "the document")


def _field_sync(hwnd: int, field: str, text: str, clear: bool, try_value: bool) -> dict:
    nodes, elems = walk(hwnd, 3000)
    wrect = _rect(hwnd)
    kinds = {"Edit", "ComboBox", "Document", "Spinner"}
    n = None
    if _norm(field) in DOC_FIELDS:
        docs = [x for x in nodes if x.kind in ("Document", "Edit") and x.visible_in(wrect)]
        n = max(docs, key=lambda x: (x.rect[2] - x.rect[0]) * (x.rect[3] - x.rect[1]), default=None)
    else:
        hits = candidates(nodes, field, kinds, wrect)[0]
        if not hits:
            labels = candidates(nodes, field, {"Text", "Group", "Pane"}, wrect)[0]
            hits = [t for t in (label_target(nodes, lb, kinds) for lb in labels) if t]
        n = hits[0] if hits else None
    if n is None:
        return {"status": "not_found", "closest": closest_names(nodes, field, kinds | {"Text"}), "named": _uia_named(nodes)}
    if n.password or CRED.search(f"{n.name} {n.help} {n.aid} {field}"):
        return {"status": "blocked", "name": n.name or field}
    if n.enabled is False:
        return {"status": "disabled", "name": n.name}
    e = elems[n.i]
    info = {"name": n.name or n.aid or n.kind, "kind": n.kind, "aid": n.aid, "rect": n.rect, "center": n.center}
    if try_value and "value" in n.pats and not n.ro:
        try:
            vp = pattern(e, "value")
            vp.SetValue(text if clear else (vp.Value or "") + text, waitTime=0)
            time.sleep(0.1)
            got = _text_of_elem(e, n)
            if _ws(text) and _ws(text) in _ws(got):
                return {"status": "set", "via": "uia-value", "value": got, **info}
        except Exception:
            pass
    try:
        control(e).SetFocus()
    except Exception:
        pass
    return {"status": "keys", **info}


def _text_of_elem(e, n: N) -> str:
    if "text" in n.pats:
        try:
            return pattern(e, "text").DocumentRange.GetText(20000) or ""
        except Exception:
            pass
    try:
        vp = pattern(e, "value")
        return (vp.Value or "") if vp else ""
    except Exception:
        return ""


def _readback_sync(hwnd: int, info: dict) -> tuple[str, bool]:
    """(text, has keyboard focus) of the field found earlier, re-found by AutomationId/name/kind nearest its old rect."""
    nodes, elems = walk(hwnd, 3000)
    same = [n for n in nodes if n.kind == info["kind"] and ((info["aid"] and n.aid == info["aid"]) or (n.name or n.aid or n.kind) == info["name"])]
    if not same:
        return "", False
    c = info["center"]
    n = min(same, key=lambda n: abs(n.center[0] - c[0]) + abs(n.center[1] - c[1]))
    return _text_of_elem(elems[n.i], n), bool(n.focus)


@action("type_into", group="ACT", summary="type into a field by label (or the editor); reads it back",
        params="""
        field s label/placeholder, or "document"
        text s what to type
        window s? part of the window title
        clear b? replace what's there (fields yes, document no)
        enter b? press Enter afterwards
        expect s? what should show afterwards
        """, cost=0.4, tier="uia", star=True, expect=True, top="field,text", fallback="Type(loc, text) or type_text(text)",
        limits="never passwords, card or ID numbers; a document is added to unless clear=true")
async def type_into(ctx: Ctx, field: str, text: str, window: str = "", clear: bool | None = None, enter: bool = False, **_) -> str:
    if _card_like(text) or CRED.search(field or ""):
        raise Fail("BLOCKED", "IO never types passwords, codes or card numbers", "ask_user (the user types it)")
    if clear is None:  # a field's value is replaced; a whole document (the user's work) is only ever added to unless asked
        clear = _norm(field) not in DOC_FIELDS
    w = _need(ctx, window)
    guard_input(ctx, w)
    try_value = not w.cls.startswith("Chrome_WidgetWin") and w.exe not in EMULATORS  # Chromium fields ignore SetValue until a key event
    res = {"status": "not_found", "named": 0}
    if w.exe not in EMULATORS:
        res = await on_uia(_field_sync, w.hwnd, field, text, clear, try_value, timeout=5.0)
    st = res["status"]
    if st == "blocked":
        raise Fail("BLOCKED", f"\"{res['name']}\" is a password or payment field", "ask_user (the user types it)")
    if st == "disabled":
        raise Fail("DISABLED", f"field \"{res['name']}\" is disabled", "fill in what it depends on first")
    if st == "set":
        if enter:
            if not await focus(w):
                return unsure(f"\"{res['name'][:40]}\" = \"{_ws(res['value'])[:60]}\" but Enter wasn't pressed: '{w.title[:40]}' isn't in front",
                              f'hotkeys(["enter"], window="{w.title[:30]}")')
            press(w, "enter")
        return ok(f"\"{res['name'][:40]}\" = \"{_ws(res['value'])[:60]}\"" + (" + Enter" if enter else ""), via="uia-value")
    if not await focus(w):
        raise Fail("NOT_FOCUSED", f"couldn't bring '{w.title[:40]}' to the front to type", "dismiss_dialog() or focus_window(...)")
    tip = await clear_tips(w) if w.exe not in EMULATORS else ""
    via = "keys"
    if st == "not_found":
        if res.get("named", 0) >= 5 and _norm(field) not in ("", "document"):
            raise Fail("NOT_FOUND", f"no field \"{field}\" in '{w.title[:40]}'" + (f"; closest: {res['closest']}" if res.get("closest") else ""),
                       f'list_controls("{w.title[:30]}", kind="field")')
        if _norm(field) not in ("", "document"):
            pt, rung = await vision_point(ctx, w, f"the {field} text field")
            await _click_xy(ctx, w, pt, "left", False)
            note_point(ctx, pt)
            via = f"{rung}+keys"
            await asyncio.sleep(0.15)
    else:
        _text, focused = await on_uia(_readback_sync, w.hwnd, res, timeout=4.0)
        if not focused:
            await _click_xy(ctx, w, res["center"], "left", False)
            await asyncio.sleep(0.1)
    if clear:
        press(w, "ctrl+a")
        await asyncio.sleep(0.05)
    how = await asyncio.to_thread(type_text_safe, text, False, w.exe, w.hwnd)
    via = via if how == "keys" else f"{via} ({how})"
    await asyncio.sleep(0.2)
    if st == "keys":
        got, _f = await on_uia(_readback_sync, w.hwnd, res, timeout=4.0)
        if _ws(text) not in _ws(got):
            return unsure(f"typed into \"{res['name'][:40]}\" but it reads \"{_ws(got)[:60]}\"", f'read_window("{w.title[:30]}") or type_into(...) again')
        if enter:
            press(w, "enter")
        return ok(f"\"{res['name'][:40]}\" = \"{_ws(got)[:60]}\"" + (" + Enter" if enter else "") + tip, via=via)
    if enter:
        press(w, "enter")
    return unsure(f"typed {len(text)} characters into '{w.title[:40]}' but couldn't read the field back{tip}", f'read_window("{w.title[:30]}", find="{text[:20]}")')


ON = {"on", "true", "yes", "1", "checked", "enable", "enabled", "check"}
OFF = {"off", "false", "no", "0", "unchecked", "disable", "disabled", "uncheck"}


def _pid_popups(pid: int, exclude: int) -> list[int]:
    """Visible top-level windows of a process, titled or not (menus, flyouts and dropdown lists have no title)."""
    out = []

    def cb(hwnd, _):
        if int(hwnd) != exclude and _u.IsWindowVisible(hwnd) and _pid(hwnd) == pid:
            l, t, r, b = _rect(hwnd)
            if r - l > 10 and b - t > 10:
                out.append(int(hwnd))
        return True

    _u.EnumWindows(_ENUM(cb), 0)
    return out


def _set_sync(hwnd: int, label: str, value: str) -> dict:
    nodes, elems = walk(hwnd, 3000)
    wrect = _rect(hwnd)
    kinds = {"CheckBox", "RadioButton", "ComboBox", "Slider", "Spinner", "Button", "ListItem", "TabItem", "Edit", "MenuItem"}
    hits = candidates(nodes, label, kinds, wrect)[0]
    if not hits:
        labels = candidates(nodes, label, {"Text", "Group"}, wrect)[0]
        hits = [t for t in (label_target(nodes, lb, kinds - {"TabItem", "ListItem"}) for lb in labels) if t]
    if not hits:
        return {"status": "not_found", "closest": closest_names(nodes, label, kinds | {"Text"})}
    n = hits[0]
    if n.enabled is False:
        return {"status": "disabled", "name": n.name}
    e, want = elems[n.i], value.strip().lower()
    base = {"name": n.name or label, "kind": n.kind, "center": n.center}
    if "toggle" in n.pats and n.kind != "ComboBox":
        desired = 1 if want in ON else 0 if want in OFF else None
        if desired is None:
            return {"status": "bad", "why": f"{n.kind.lower()} \"{n.name}\" takes on or off", **base}
        tp = pattern(e, "toggle")
        for _ in range(2):
            if tp.ToggleState == desired:
                break
            tp.Toggle(waitTime=0)
            time.sleep(0.15)
        got = tp.ToggleState
        return {"status": "done" if got == desired else "mismatch", "got": {0: "off", 1: "on", 2: "mixed"}.get(got, str(got)), "via": "uia-toggle", **base}
    if "select" in n.pats and n.kind in ("RadioButton", "ListItem", "TabItem"):
        sp = pattern(e, "select")
        sp.Select(waitTime=0)
        time.sleep(0.15)
        return {"status": "done" if sp.IsSelected else "mismatch", "got": "selected" if sp.IsSelected else "not selected", "via": "uia-select", **base}
    if n.kind == "ComboBox":
        ep = pattern(e, "expand") if "expand" in n.pats else None
        if ep:
            ep.Expand(waitTime=0)
            time.sleep(0.35)
        pool: list = []
        try:
            sub_nodes, sub_elems = walk(0, 400, root_elem=e)
            pool.append((sub_nodes, sub_elems))
        except Exception:
            pass
        for pop in _pid_popups(_pid(hwnd), hwnd):
            try:
                pool.append(walk(pop, 800))
            except Exception:
                pass
        for pn, pe in pool:
            items = candidates(pn, value, {"ListItem", "MenuItem", "Text", "DataItem"})[0]
            if items:
                it = items[0]
                done = ""
                try:
                    if "select" in it.pats:
                        pattern(pe[it.i], "select").Select(waitTime=0)
                        done = "uia-select"
                    elif "invoke" in it.pats:
                        pattern(pe[it.i], "invoke").Invoke(waitTime=0)
                        done = "uia-invoke"
                except Exception:
                    done = ""
                if not done:
                    return {"status": "click_item", "item": it.center, **base}
                time.sleep(0.2)
                try:
                    if ep and ep.ExpandCollapseState == 1:
                        ep.Collapse(waitTime=0)
                except Exception:
                    pass
                time.sleep(0.15)
                got = _combo_value(e)
                return {"status": "done" if _norm(value) in _norm(got) or not got else "mismatch", "got": got or it.name, "via": done, **base}
        if ep:
            try:
                ep.Collapse(waitTime=0)
            except Exception:
                pass
        if "value" in n.pats and not n.ro:
            pattern(e, "value").SetValue(value, waitTime=0)
            time.sleep(0.15)
            got = _combo_value(e)
            return {"status": "done" if _norm(value) in _norm(got) else "mismatch", "got": got, "via": "uia-value", **base}
        return {"status": "keys", **base}
    if "range" in n.pats:
        rp = pattern(e, "range")
        try:
            v = float(re.findall(r"-?\d+(?:\.\d+)?", value)[0])
        except IndexError:
            return {"status": "bad", "why": f"{n.kind.lower()} \"{n.name}\" takes a number", **base}
        v = max(rp.Minimum, min(rp.Maximum, v))
        rp.SetValue(v, waitTime=0)
        time.sleep(0.15)
        got = rp.Value
        return {"status": "done" if abs(got - v) < 1e-6 or abs(got - v) <= abs(rp.Maximum - rp.Minimum) / 100 else "mismatch", "got": f"{got:g}",
                "via": "uia-range", **base}
    if "value" in n.pats and not n.ro:
        pattern(e, "value").SetValue(value, waitTime=0)
        time.sleep(0.12)
        got = _text_of_elem(e, n)
        return {"status": "done" if _norm(value) in _norm(got) else "mismatch", "got": got[:60], "via": "uia-value", **base}
    return {"status": "keys", **base}


def _combo_value(e) -> str:
    try:
        vp = pattern(e, "value")
        if vp and vp.Value:
            return vp.Value
    except Exception:
        pass
    try:
        c = control(e)
        from windows_mcp import uia
        sel = c.GetPattern(uia.PatternId.SelectionPattern)
        items = sel.GetSelection() if sel else []
        return items[0].Name if items else ""
    except Exception:
        return ""


SENSITIVE_SETTING = re.compile(r"security|privacy|defender|firewall|account|sign[- ]?in|update|uac|user account|password|encryption|bitlocker", re.I)


@action("set_control", group="ACT", summary="set a checkbox, toggle, radio, dropdown, slider or spin box",
        params="""
        label s the control's label
        value s on/off, an option's text, or a number
        window s? part of the window title
        expect s? what should show afterwards
        """, cost=0.5, tier="uia", star=True, expect=True, top="label,value", fallback="click(target)",
        risky=lambda a, c: _set_risky(a, c), limits="reads the state back; security/privacy settings ask the user first")
async def set_control(ctx: Ctx, label: str, value: str, window: str = "", **_) -> str:
    w = _need(ctx, window)
    guard_input(ctx, w)
    res = await on_uia(_set_sync, w.hwnd, label, value, timeout=6.0)
    st = res["status"]
    if st == "not_found":
        raise Fail("NOT_FOUND", f"no control \"{label}\" in '{w.title[:40]}'" + (f"; closest: {res['closest']}" if res.get("closest") else ""),
                   f'list_controls("{w.title[:30]}") or scroll_until("{label}")')
    if st == "disabled":
        raise Fail("DISABLED", f"\"{res['name']}\" is disabled", "find_control(...) to see why")
    if st == "bad":
        raise Fail("BAD_ARGS", res["why"], f'set_control("{label}", "on")')
    name = f"{res['kind'].lower()} \"{res['name'][:40]}\""
    if st == "done":
        return ok(f"{name} = {res['got']}", via=res["via"])
    if st == "click_item":
        await focus(w)
        await _click_xy(ctx, w, res["item"], "left", False)
        await asyncio.sleep(0.3)
        return ok(f"{name}: clicked option \"{value}\"", via="uia+click")
    if st == "keys":
        if not await focus(w):
            raise Fail("NOT_FOCUSED", f"couldn't bring '{w.title[:40]}' to the front", "focus_window(...)")
        await _click_xy(ctx, w, res["center"], "left", False)
        await asyncio.sleep(0.2)
        await asyncio.to_thread(type_text_safe, value, True, w.exe, w.hwnd)
        await asyncio.sleep(0.3)
        return unsure(f"typed \"{value}\" + Enter into {name} but couldn't read it back", f'find_control("{w.title[:30]}", "{label}")')
    # mismatch: one real click, then read again
    if res["kind"] in ("CheckBox", "Button", "RadioButton") and await focus(w):
        await _click_xy(ctx, w, res["center"], "left", False)
        await asyncio.sleep(0.3)
        again = await on_uia(_set_sync, w.hwnd, label, value, timeout=6.0)
        if again["status"] == "done":
            return ok(f"{name} = {again['got']}", via="uia-click")
    raise Fail("NO_CHANGE", f"{name} still reads {res.get('got', '?')!r}, not {value!r}", f'find_control("{w.title[:30]}", "{label}")')


def _set_risky(args: dict, ctx: Ctx) -> str:
    w = resolve(ctx, str(args.get("window") or ""))
    if w and (w.exe == "systemsettings.exe" or w.title == "Settings") and SENSITIVE_SETTING.search(f"{args.get('label', '')}"):
        return f"change the setting \"{args.get('label')}\" to {args.get('value')}"
    return ""


def _menu_sync(hwnd: int, name: str, leaf: bool, popups_first: bool) -> dict:
    roots = _pid_popups(_pid(hwnd), hwnd)
    roots = (roots + [hwnd]) if popups_first else ([hwnd] + roots)
    level_names: list = []
    for root in roots:
        try:
            nodes, elems = walk(root, 2500)
        except Exception:
            continue
        items = [n for n in nodes if n.kind in ("MenuItem", "TabItem") and n.enabled is not False]
        if items and not level_names:
            level_names = list(dict.fromkeys(n.name for n in items if n.name))[:20]
        hits = candidates(items, name)[0]
        if not hits:
            continue
        n, e = hits[0], elems[hits[0].i]
        try:
            if not leaf and "expand" in n.pats:
                pattern(e, "expand").Expand(waitTime=0)
                return {"status": "acted", "name": n.name, "via": "expand"}
            if "invoke" in n.pats:
                pattern(e, "invoke").Invoke(waitTime=0)
                return {"status": "acted", "name": n.name, "via": "invoke"}
            if "expand" in n.pats:
                pattern(e, "expand").Expand(waitTime=0)
                return {"status": "acted", "name": n.name, "via": "expand"}
            if "select" in n.pats:
                pattern(e, "select").Select(waitTime=0)
                return {"status": "acted", "name": n.name, "via": "select"}
        except Exception:
            pass
        return {"status": "real", "name": n.name, "center": n.center}
    return {"status": "not_found", "items": level_names}


@action("select_menu", group="ACT", summary='run a menu command by its path, e.g. "File > Save As"',
        params="""
        path s menu path like "File > Save As"
        window s? part of the window title
        expect s? what should show afterwards
        """, cost=1.2, tier="uia", star=True, expect=True, top="path", fallback='click per level, or Shortcut("alt+f")',
        limits="ribbons read as tab > button; context menus: click(target, button=\"right\") first")
async def select_menu(ctx: Ctx, path: str, window: str = "", **_) -> str:
    levels = [p.strip() for p in re.split(r"\s*(?:>|->|→|/)\s*", path) if p.strip()]
    if not levels:
        raise Fail("BAD_ARGS", "path is like \"File > Save As\"", 'select_menu("File > Save As")')
    w = _need(ctx, window)
    guard_input(ctx, w)
    if not await focus(w):
        raise Fail("NOT_FOCUSED", f"couldn't bring '{w.title[:40]}' to the front", "dismiss_dialog() or focus_window(...)")
    tip = await clear_tips(w, strict=False)
    before_wins = {x.hwnd for x in windows(owned=True)}
    before = await snapshot(w)
    done: list[str] = []
    for i, name in enumerate(levels):
        leaf = i == len(levels) - 1
        res: dict = {"status": "not_found", "items": []}
        for attempt in range(2):
            t_end = time.time() + (0.2 if i == 0 else 1.5)
            while True:
                res = await on_uia(_menu_sync, w.hwnd, name, leaf, i > 0, timeout=5.0)
                if res["status"] != "not_found" or time.time() >= t_end:
                    break
                await asyncio.sleep(0.25)
            if res["status"] != "not_found" or i == 0 or attempt:
                break
            # the submenu didn't open (a tip popped up, or the first expand was eaten): open the level above again
            tip = tip or await clear_tips(w, strict=False)
            await on_uia(_menu_sync, w.hwnd, levels[i - 1], False, i - 1 > 0, timeout=5.0)
            await asyncio.sleep(0.3)
        if res["status"] == "not_found":
            if done and w.exe not in EMULATORS and _is_front(w.hwnd):
                send_combo("esc")  # close the menus this opened
                send_combo("esc")
            under = f" under \"{done[-1]}\"" if done else ""
            items = ", ".join(res["items"][:15]) or "nothing IO can read"
            best = difflib.get_close_matches(name, res["items"], n=1, cutoff=0.4)
            fix = " > ".join(done + [best[0]] + levels[i + 1:]) if best else ""
            raise Fail("NOT_FOUND", f"\"{name}\" not found{under}; it has: {items}", f'select_menu("{fix}")' if fix else f'list_controls("{w.title[:30]}", kind="menu")')
        if res["status"] == "real":
            await _click_xy(ctx, w, res["center"], "left", False)
        done.append(res["name"])
        await asyncio.sleep(0.15)
    what = await wait_change(w, before, settle=0.3, limit=2.0, new_from=before_wins)
    if not what:
        return unsure(f"ran {' > '.join(done)} in '{w.title[:40]}' but nothing visibly changed", "check_screen(...) or list_windows()")
    return ok(f"ran {' > '.join(done)} in '{w.title[:40]}' ({what}{' changed' if what in ('tree', 'screen') else ''}){tip}", via="uia")


def _text_focus_sync(hwnd: int) -> str:
    """Typing needs an editable control: if the window's keyboard focus is on a tab, a menu or a button (it lands there
    after dialogs and window moves), focus the window's main editor. Returns what was focused ('' if nothing changed)."""
    from windows_mcp import uia
    c = uia.GetFocusedControl()
    if c is not None and c.ControlTypeName.removesuffix("Control") in ("Edit", "Document", "ComboBox") and c.ProcessId == _pid(hwnd):
        return ""
    nodes, elems = walk(hwnd, 2000)
    wrect = _rect(hwnd)
    docs = [n for n in nodes if n.kind in ("Document", "Edit") and n.visible_in(wrect) and n.enabled is not False]
    if not docs:
        return ""
    n = max(docs, key=lambda x: (x.rect[2] - x.rect[0]) * (x.rect[3] - x.rect[1]))
    try:
        control(elems[n.i]).SetFocus()
        return n.name or n.kind
    except Exception:
        return ""


async def ensure_text_focus(w: W) -> str:
    try:
        return await on_uia(_text_focus_sync, w.hwnd, timeout=3.0)
    except Exception:
        return ""


BAD_KEYS_ALWAYS = {"win+r", "win+x", "win+l", "ctrl+alt+delete", "ctrl+alt+del"}
BAD_KEYS_GAME = {"esc", "escape", "back", "browser_back", "alt+f4"}


@action("hotkeys", group="ACT", summary='press keys in order, e.g. ["ctrl+l", "text:C:\\\\x", "enter"]',
        params="""
        keys a e.g. ["ctrl+s", "text:hi", "wait:1"]
        window s? part of the window title
        expect s? what should show afterwards
        """, cost=0.5, tier="exact", star=True, expect=True, top="keys", fallback="Shortcut(shortcut)",
        risky=lambda a, c: _keys_close_risky(a, c) if closes_by_keys("hotkeys", a) else "",
        limits="stops if focus moves; no Esc/Back in games; launching goes through open_app")
async def hotkeys(ctx: Ctx, keys: list, window: str = "", **_) -> str:
    items = [str(k) for k in keys if str(k).strip()]
    if not items:
        raise Fail("BAD_ARGS", "keys is a list like [\"ctrl+s\"]", 'hotkeys(["ctrl+s"])')
    w = _need(ctx, window)
    guard_input(ctx, w)
    game = w.exe in EMULATORS or ctx.loop
    for k in items:
        low = re.sub(r"\s+", "", k.lower())
        if low in BAD_KEYS_ALWAYS:
            raise Fail("BLOCKED", f"{k} isn't allowed", "open_app(name) or open_path(path)")
        if low == "alt+space":  # the system menu: its Move/Size mode grabs the mouse pointer and parks it mid-window
            raise Fail("BLOCKED", "alt+space (the window menu) isn't used", f'window_state("{window or w.title[:30]}", "max"|"min"|"restore"|"left"|"right")')
        if game and not low.startswith("text:") and low in BAD_KEYS_GAME:
            raise Fail("BLOCKED", f"{k} can close the game here", "dismiss_dialog() or click(\"the X close button\", how=\"vision\")")
    if not await focus(w):
        raise Fail("NOT_FOCUSED", f"couldn't bring '{w.title[:40]}' to the front", "dismiss_dialog() or focus_window(...)")
    tip = await clear_tips(w, strict=False) if not game else ""  # a popup that asks something may be what the keys are for
    editing = re.fullmatch(r"(ctrl\+)?(home|end|a|z|y|enter|backspace|delete|up|down|left|right|pageup|pagedown)|text:.*", items[0].lower().replace(" ", ""), re.S)
    if editing and not game:
        await ensure_text_focus(w)
    before_wins = {x.hwnd for x in windows(owned=True)}
    before = await snapshot(w)
    home = _cursor()
    try:
        await _press_keys(ctx, w, items, game, tip)
    finally:
        if _cursor() != home:  # a key that grabbed the pointer (a move/size mode): it goes back where the user had it
            _u.SetCursorPos(*home)
    now = fg()
    what = await wait_change(w, before, settle=0.25, limit=0.8, new_from=before_wins)
    return ok(f"pressed {', '.join(items)[:120]} in '{w.title[:40]}'" + (f" ({what} changed)" if what in ("tree", "screen") else f" ({what})" if what else "")
              + tip, now=f"'{now.title[:50]}'" if now else "")


async def _press_keys(ctx: Ctx, w: W, items: list, game: bool, tip: str) -> None:
    for step, k in enumerate(items):
        if k.lower().startswith("wait:"):
            try:
                await asyncio.sleep(max(0.0, min(5.0, float(k[5:]))))
            except ValueError:
                raise Fail("BAD_ARGS", f"{k}: wait takes seconds, like wait:0.5")
            continue
        if not _is_front(w.hwnd):
            holder = fg()
            raise Fail("NOT_FOCUSED", f"focus moved to '{holder.title[:40] if holder else '?'}' after {step} of {len(items)} steps", "hotkeys again from there")
        if k.lower().startswith("text:"):
            if not game:  # tips can pop up seconds after an app starts, in the middle of a sequence
                tip = tip or await clear_tips(w, strict=False)
                await ensure_text_focus(w)
            await asyncio.to_thread(type_text_safe, k[5:], False, w.exe, w.hwnd)
        else:
            problem = await asyncio.to_thread(send_combo, k)
            if problem and ctx.win is not None:
                res = _h().text_of(await ctx.win.call_tool("Shortcut", {"shortcut": k}))
                problem = "" if "error" not in res.lower() else res[:100]
            if problem:
                raise Fail("BAD_ARGS", f"{problem} (after {step} steps)", 'hotkeys(["ctrl+s"])')
        await asyncio.sleep(0.12)


def _scroll_sync(hwnd: int, target: str, direction: str, step: bool) -> dict:
    """One scroll attempt: found (and scrolled into view), scrolled a page, or no scrollable container."""
    from windows_mcp import uia
    nodes, elems = walk(hwnd, 3000)
    wrect = _rect(hwnd)
    t = target.strip().lower()
    scrollers = [n for n in nodes if "scroll" in n.pats]
    if t in ("top", "bottom", "end", "start"):
        if not scrollers:
            return {"status": "no_scroll"}
        n = max(scrollers, key=lambda n: (n.rect[2] - n.rect[0]) * (n.rect[3] - n.rect[1]))
        sp = pattern(elems[n.i], "scroll")
        sp.SetScrollPercent(-1, 0 if t in ("top", "start") else 100, waitTime=0)
        return {"status": "found", "how": "scroll percent"}
    hits = candidates(nodes, target, None, None)[0]
    if hits:
        n = hits[0]
        if n.visible_in(wrect):
            return {"status": "found", "center": n.center, "how": "already visible"}
        if "scrollitem" in n.pats:
            pattern(elems[n.i], "scrollitem").ScrollIntoView(waitTime=0)
            time.sleep(0.15)
            r = live_rect(elems[n.i])
            return {"status": "found", "center": ((r[0] + r[2]) // 2, (r[1] + r[3]) // 2), "how": "scroll into view"}
    for c in [n for n in nodes if "container" in n.pats]:  # virtualised lists know items they haven't drawn yet
        try:
            ic = control(elems[c.i]).GetPattern(uia.PatternId.ItemContainerPattern)
            item = ic.FindItemByProperty(None, _props().NameProperty, target) if ic else None
            if item:
                vp = item.GetPattern(uia.PatternId.VirtualizedItemPattern)
                if vp:
                    vp.Realize(waitTime=0)
                sip = item.GetPattern(uia.PatternId.ScrollItemPattern)
                if sip:
                    sip.ScrollIntoView(waitTime=0)
                r = item.BoundingRectangle
                return {"status": "found", "center": ((r.left + r.right) // 2, (r.top + r.bottom) // 2), "how": "realised item"}
        except Exception:
            continue
    if not step:
        return {"status": "missing", "scrollers": len(scrollers)}
    if scrollers:
        n = max(scrollers, key=lambda n: (n.rect[2] - n.rect[0]) * (n.rect[3] - n.rect[1]))
        sp = pattern(elems[n.i], "scroll")
        amt = uia.ScrollAmount.LargeIncrement if direction in ("down", "right") else uia.ScrollAmount.LargeDecrement
        try:
            if direction in ("left", "right"):
                sp.Scroll(amt, uia.ScrollAmount.NoAmount, waitTime=0)
            else:
                sp.Scroll(uia.ScrollAmount.NoAmount, amt, waitTime=0)
            return {"status": "scrolled", "center": n.center}
        except Exception:
            return {"status": "wheel", "center": n.center}
    return {"status": "wheel", "center": None}


@action("scroll_until", group="ACT", summary="scroll until some text is visible, or to the top/bottom",
        params="""
        target s text to bring into view, or top/bottom
        window s? part of the window title
        direction s? down|up|left|right
        max i? most scroll steps (15)
        """, cost=1.5, tier="uia", star=True, top="target", fallback="Scroll(loc, direction)", timeout=60,
        limits="games: vision checks every 2 steps, at most 8")
async def scroll_until(ctx: Ctx, target: str, window: str = "", direction: str = "down", max: int = 15, **_) -> str:
    w = _need(ctx, window)
    guard_input(ctx, w)
    steps = builtins_max(1, min(int(max or 15), 40))
    no_uia = w.exe in EMULATORS or ctx.loop
    area = _area_rect(ctx, w)
    if no_uia:
        steps = min(steps, 8)
        for k in range(steps):
            await game_swipe(ctx, w, {"down": "up", "up": "down", "left": "right", "right": "left"}[direction]) if w.exe in EMULATORS else \
                await asyncio.to_thread(mouse_wheel, (area[0] + area[2]) // 2, (area[1] + area[3]) // 2, -3 if direction == "down" else 3)
            await asyncio.sleep(0.4)
            if k % 2 == 1:
                ans, why = await yes_no(ctx, f"Is {target} visible?", w)
                if ans == "yes":
                    return ok(f"{target!r} is visible after {k + 1} scrolls ({why})", via="vision")
        raise Fail("NOT_FOUND", f"{target!r} not seen after {steps} scrolls", f'check_screen("where is {target[:30]}?")')
    last = await sig_of(_rect(w.hwnd))
    for k in range(steps + 1):
        res = await on_uia(_scroll_sync, w.hwnd, target, direction, k > 0, timeout=5.0)
        if res["status"] == "found":
            return ok(f"{target!r} is in view" + (f" at {res['center']}" if res.get("center") else "") + f" after {k} steps", via=res["how"])
        if res["status"] == "no_scroll":
            if await focus(w):
                press(w, "ctrl+home" if target.lower() in ("top", "start") else "ctrl+end")
                return ok(f"pressed {'Ctrl+Home' if target.lower() in ('top', 'start') else 'Ctrl+End'} in '{w.title[:40]}'", via="keys")
            raise Fail("NOT_FOCUSED", f"couldn't bring '{w.title[:40]}' to the front")
        if res["status"] == "wheel":
            c = res.get("center") or ((area[0] + area[2]) // 2, (area[1] + area[3]) // 2)
            await asyncio.to_thread(mouse_wheel, c[0], c[1], -5 if direction == "down" else 5)
        await asyncio.sleep(0.3)
        now = await sig_of(_rect(w.hwnd))
        if k > 0 and now is not None and last is not None and not changed(last, now):
            raise Fail("NOT_FOUND", f"scrolled to the end ({k} steps) without seeing {target!r}", f'read_window("{w.title[:30]}", find="{target[:30]}")')
        last = now
    raise Fail("NOT_FOUND", f"{target!r} not found after {steps} scroll steps", f'read_window("{w.title[:30]}", find="{target[:30]}")')


def _texts_sync(hwnd: int) -> str:
    """Every name and value in the window, documents read through TextPattern (cached values can be stale), lower case."""
    nodes, elems = walk(hwnd, 2000)
    parts = []
    for n in nodes:
        parts.append(n.name)
        if n.kind in ("Document", "Edit") and "text" in n.pats:
            parts.append(_text_of_elem(elems[n.i], n))
        elif n.value:
            parts.append(n.value)
    return "\n".join(p for p in parts if p).lower()


@action("wait_until", group="ACT", summary="wait for text, a window, a file or the screen to settle",
        params="""
        cond s text_appears|text_gone|window_appears|window_gone|element_enabled|screen_still|screen_changes|file_exists|vision
        target s? text, title, control, path or question
        window s? part of the window title
        timeout n? seconds (15)
        """, cost=1.0, tier="uia", star=True, top="cond,target?", fallback="wait(seconds)", timeout=10, hide=("timeout",),
        limits="Stop ends it at once; wait(n) is for game timers")
async def wait_until(ctx: Ctx, cond: str, target: str = "", window: str = "", timeout: float = 15, **_) -> str:
    timeout = builtins_max(0.5, min(float(timeout or 15), 600))
    if cond in ("text_appears", "text_gone", "element_enabled", "file_exists", "window_appears", "window_gone", "vision") and not target:
        raise Fail("BAD_ARGS", f"{cond} needs target", f'wait_until("{cond}", target="...")')
    t0 = time.time()
    t_end = t0 + timeout
    w = None
    if cond in ("text_appears", "text_gone", "element_enabled", "screen_still", "screen_changes", "vision"):
        w = resolve(ctx, window)
        if w is None and cond != "vision":
            raise Fail("NOT_FOUND", f"no window matching {window!r}", "list_windows()")
        if w is not None and cond in ("text_appears", "text_gone", "element_enabled"):
            guard_read(ctx, w)
    low = target.lower()
    path = _path(target) if cond == "file_exists" else None
    base = await sig_of(_area_rect(ctx, w)) if cond in ("screen_still", "screen_changes") else None
    noise = None
    if cond == "screen_changes" and base is not None:
        await asyncio.sleep(0.15)
        second = await sig_of(_area_rect(ctx, w))
        noise = sig_diff(base, second)
    still_since = time.time()
    last_seen = ""
    while True:
        elapsed = round(time.time() - t0, 1)
        if cond in ("text_appears", "text_gone", "element_enabled"):
            if not _alive(w.hwnd):
                if cond == "text_gone":
                    return ok(f"'{w.title[:40]}' closed after {elapsed}s")
                raise Fail("NOT_FOUND", f"'{w.title[:40]}' closed while waiting")
            if cond == "element_enabled":
                res = await on_uia(_find_sync, w.hwnd, target, None, timeout=4.0)
                if res["hits"] and res["hits"][0]["enabled"]:
                    return ok(f"\"{res['hits'][0]['name'][:40]}\" is enabled after {elapsed}s")
            else:
                text = await on_uia(_texts_sync, w.hwnd, timeout=4.0)
                last_seen = text.strip().splitlines()[-1][:80] if text.strip() else ""
                if (low in text) == (cond == "text_appears"):
                    return ok(f"\"{target[:40]}\" {'appeared' if cond == 'text_appears' else 'is gone'} after {elapsed}s in '{w.title[:40]}'")
            await asyncio.sleep(0.25)
        elif cond in ("window_appears", "window_gone"):
            hit = [x for x in windows(owned=True) if low in x.title.lower()]
            if hit and cond == "window_appears":
                return ok(f"'{hit[0].title[:60]}' is open after {elapsed}s")
            if not hit and cond == "window_gone":
                return ok(f"no window titled like {target!r} after {elapsed}s")
            await asyncio.sleep(0.15)
        elif cond == "file_exists":
            if path and path.exists():
                return ok(f"{path} exists ({_size(path)}) after {elapsed}s")
            await asyncio.sleep(0.3)
        elif cond in ("screen_still", "screen_changes"):
            await asyncio.sleep(0.2)
            now = await sig_of(_area_rect(ctx, w))
            if cond == "screen_changes" and changed(base, now, noise):
                return ok(f"the screen changed after {elapsed}s")
            if cond == "screen_still":
                if changed(base, now):
                    base, still_since = now, time.time()
                elif time.time() - still_since >= 0.6:
                    return ok(f"the screen is still after {elapsed}s")
        elif cond == "vision":
            ans, why = await yes_no(ctx, target, w)
            if ans == "yes":
                return ok(f"yes after {elapsed}s — {why}")
            last_seen = why
            await asyncio.sleep(2.0)
        else:
            raise Fail("BAD_ARGS", f"unknown cond {cond!r}", "wait_until(\"window_appears\", target=\"Save\")")
        if time.time() >= t_end:
            now_titles = ", ".join(f"'{x.title[:30]}'" for x in windows()[:4])
            seen = f"last text: {last_seen!r}" if last_seen else f"open: {now_titles}"
            raise Fail("TIMEOUT", f"{cond} {target[:40]!r} not met after {timeout:g}s; now: {seen}", f'wait_until("{cond}", target="{target[:30]}", timeout={int(timeout * 2)})')


# ======================================================================================================================
# L2 · SEE: vision, several targets
# ======================================================================================================================

@action("find_all", group="SEE", summary="every place something appears on screen, as points",
        params="""
        description s what to find
        window s? part of the window title
        max i? most points (10)
        """, cost=5.0, tier="vision", top="description", fallback="find_on_screen(description)",
        limits="slower than find_on_screen: use it when there are several")
async def find_all(ctx: Ctx, description: str, window: str = "", max: int = 10, **_) -> str:
    w = resolve(ctx, window) if (window or ctx.focus) else None
    if w is not None:
        guard_read(ctx, w)
        await focus(w)
        await asyncio.sleep(0.3)
    rect = _area_rect(ctx, w)
    img = await asyncio.to_thread(grab, rect)
    reply = await asyncio.to_thread(vlm, img, f"Find every {description} on the screenshot. Answer only with JSON like "
                                         '{"points": [[x, y], ...]}, the centre of each, x and y on a 0-1000 scale across the image; '
                                         '{"points": []} if there are none.', 400)
    pts = []
    for a, b in re.findall(r"\[\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\]", reply):
        fx, fy = float(a), float(b)
        if 0 <= fx <= 1000 and 0 <= fy <= 1000:
            pts.append((round(rect[0] + fx / 1000 * (rect[2] - rect[0])), round(rect[1] + fy / 1000 * (rect[3] - rect[1]))))
    pts = pts[:builtins_max(1, min(int(max or 10), 30))]
    for p in pts:
        note_point(ctx, p)
    if not pts:
        return ok(f"none: no {description} seen")
    return ok(f"{len(pts)} found: " + " ".join(f"({x},{y})" for x, y in pts))


# ======================================================================================================================
# L2 · FILE: files without UI (in process; PowerShell only for the Search index and the Recycle Bin)
# ======================================================================================================================

KNOWN_FOLDERS = {"desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641", "documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
                 "downloads": "374DE290-123F-4565-9164-39C4925E467B", "pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
                 "music": "4BD8D571-6D19-48D3-BE97-422220080E43", "videos": "18989B1D-99B5-455B-841C-AB7C74E4DDFC"}


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", wt.BYTE * 8)]


def known_folder(name: str) -> Path:
    """Desktop, Documents, ... where they really are (OneDrive redirection moves Desktop and Documents)."""
    name = name.lower().removeprefix("my ").strip()
    if name in ("home", "profile", "user"):
        return Path(os.environ.get("USERPROFILE", str(Path.home())))
    if name in ("temp", "tmp"):
        return Path(os.environ.get("TEMP", os.environ.get("TMP", ".")))
    gid = KNOWN_FOLDERS.get(name.rstrip("s") + "s") or KNOWN_FOLDERS.get(name)
    if gid:
        g = _GUID()
        ctypes.oledll.ole32.CLSIDFromString(f"{{{gid}}}", ctypes.byref(g))
        out = ctypes.c_wchar_p()
        try:
            ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out))
            if out.value:
                return Path(out.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(out)
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / name.capitalize()


def _path(p: str) -> Path:
    # PowerShell's $env:NAME too: models that just ran PowerShell write paths the same way in file actions
    s = re.sub(r"(?i)\$env:(\w+)", r"%\1%", str(p or "").strip().strip("\"'"))
    s = os.path.expanduser(os.path.expandvars(s))
    m = re.match(r"(?i)^(?:my\s+|the\s+)?(desktop|documents|downloads|pictures|music|videos|home|temp)(?:\s+folder)?(?:\s*[\\/](.*))?$", s)
    if m:
        return _real(known_folder(m.group(1)) / (m.group(2) or ""))
    path = Path(s)
    return _real(path if path.is_absolute() else known_folder("home") / path)


def _size(p: Path) -> str:
    try:
        return _human(p.stat().st_size) if p.is_file() else "folder"
    except OSError:
        return "?"


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _real(p: Path) -> Path:
    """The path with '..' and '.' resolved (no symlink lookups): %USERPROFILE%\\..\\..\\ProgramData is C:\\ProgramData."""
    try:
        return Path(os.path.abspath(str(p)))
    except (OSError, ValueError):
        return p


def _private(p: Path) -> bool:
    """IO's own data and logs (the NVIDIA key, the Chrome token, task history): never read, listed or found for a task,
    so a web page that talks the agent into it gets nothing."""
    s = str(_real(p)).lower()
    for d in (_h().HERE / "data", _h().HERE / "logs"):
        r = str(_real(d)).lower().rstrip("\\")
        if s == r or s.startswith(r + "\\"):
            return True
    return False


def _forbidden(p: Path) -> bool:
    roots = [os.environ.get("SystemRoot", r"C:\Windows"), os.environ.get("ProgramFiles", r"C:\Program Files"),
             os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), os.environ.get("ProgramData", r"C:\ProgramData")]
    s = str(_real(p)).lower()
    return any(r and (s == r.lower() or s.startswith(r.lower().rstrip("\\") + "\\")) for r in roots)


def writable(ctx: Ctx, p: Path) -> None:
    """Writes only under the user's profile, %TEMP%, or a path the request names; never Windows or Program Files."""
    p = _real(p)
    if _forbidden(p):
        raise Fail("BLOCKED", f"IO never writes under {p.anchor}{p.parts[1] if len(p.parts) > 1 else ''}", "a path in the user's folders")
    s = str(p).lower()
    # IO's own program is read-only to IO (it sits under the user's profile, so the rule below would allow it): new
    # abilities are built in the workshop folder, which is the one place inside it a task may write
    own = str(_real(HERE)).lower().rstrip("\\")
    if (s == own or s.startswith(own + "\\")) and not s.startswith(str(_real(HERE / "workshop")).lower() + "\\"):
        raise Fail("BLOCKED", f"{p} is part of IO's own program, which IO reads but never changes",
                   "about_io() for how it works; propose_tool(...) for a new ability, built in the workshop")
    allowed = [os.environ.get("USERPROFILE", ""), os.environ.get("TEMP", ""), os.environ.get("TMP", "")]
    if any(a and s.startswith(str(_real(Path(a))).lower().rstrip("\\") + "\\") for a in allowed):
        return
    req = _clean(ctx.request or "").lower().replace("/", "\\")
    parent = str(p.parent).lower()
    if s in req or (len(p.parent.parts) > 1 and re.search(re.escape(parent) + r"(\\|\s|$|[\"'.,])", req)):
        return  # the request names this path, or the folder it goes in (never just a drive root)
    raise Fail("BLOCKED", f"{p} is outside the user's folders and the request didn't name it", "ask_user")


def _listing_sync(folder: Path, pattern: str, sort: str, n: int, recurse: bool) -> tuple[list, bool]:
    t_end = time.time() + 20
    entries, partial = [], False
    pat = pattern or "*"
    if "*" not in pat and "?" not in pat:
        pat = f"*{pat}*"
    it = folder.rglob(pat) if recurse else folder.glob(pat)
    base_depth = len(folder.parts)
    for p in it:
        if time.time() > t_end:
            partial = True
            break
        if recurse and len(p.parts) - base_depth > 3:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        entries.append((p, st.st_size if p.is_file() else -1, st.st_mtime))
        if len(entries) > 20000:
            partial = True
            break
    key = {"newest": lambda e: -e[2], "oldest": lambda e: e[2], "largest": lambda e: -e[1], "name": lambda e: str(e[0]).lower()}[sort]
    entries.sort(key=key)
    return entries[:n], partial or len(entries) > n


@action("list_files", group="FILE", summary="files in a folder: name, size, modified; newest/largest first",
        params="""
        folder s a path, or Desktop/Documents/Downloads/temp
        pattern s? like *.pdf or report
        sort s? newest|oldest|largest|name
        n i? how many (20)
        recurse b? include subfolders (3 deep)
        """, cost=0.4, star=True, top="folder", fallback='PowerShell("Get-ChildItem ...")')
async def list_files(ctx: Ctx, folder: str, pattern: str = "", sort: str = "newest", n: int = 20, recurse: bool = False, **_) -> str:
    p = _path(folder)
    if _private(p):
        raise Fail("BLOCKED", "that is IO's own data folder", "ask_user")
    if not p.exists():
        raise Fail("NOT_FOUND", f"{p} doesn't exist", f'find_file("{Path(folder).name}")')
    if p.is_file():
        return ok(f"{p} is a file: {_size(p)}, modified {dt.datetime.fromtimestamp(p.stat().st_mtime):%Y-%m-%d %H:%M}")
    rows, more = await asyncio.to_thread(_listing_sync, p, pattern, sort, builtins_max(1, min(int(n or 20), 200)), recurse)
    if not rows:
        return ok(f"{p} has no {'files matching ' + repr(pattern) if pattern else 'files'}")
    lines = [f"{str(e[0].relative_to(p)) + (chr(92) if e[1] < 0 else '')}  {_human(e[1]) if e[1] >= 0 else 'folder'}  "
             f"{dt.datetime.fromtimestamp(e[2]):%Y-%m-%d %H:%M}" for e in rows]
    return ok(f"{p} ({sort} first{', more not shown' if more else ''}):\n" + "\n".join(lines))


SKIP_DIRS = {"node_modules", "appdata", "venv", "__pycache__", "site-packages", "windows", "winsxs"}  # huge, and never where a person's file is


def _walk_find_sync(root: Path, name: str, days: float, limit_s: float = 25) -> tuple[list, bool]:
    import fnmatch
    pat = name.lower()
    wild = "*" in pat or "?" in pat
    since = time.time() - days * 86400 if days else 0
    t_end, found = time.time() + limit_s, []
    base = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        if time.time() > t_end:
            return found, True
        depth = len(Path(dirpath).parts) - base
        if depth >= 6:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "$")) and d.lower() not in SKIP_DIRS]
        for f in filenames + dirnames:
            low = f.lower()
            if (fnmatch.fnmatch(low, pat) if wild else pat in low):
                full = Path(dirpath) / f
                try:
                    mt = full.stat().st_mtime
                except OSError:
                    continue
                if mt >= since:
                    found.append((full, mt))
                    if len(found) >= 50:
                        return found, True
    return found, False


@action("find_file", group="FILE", summary="where is a file: the Search index first, then a folder walk",
        params="""
        name s file name or part of it (wildcards ok)
        where s? folder to search (home)
        newer_than_days n? only files changed in the last N days
        """, cost=1.0, star=True, top="name", fallback='PowerShell("Get-ChildItem -Recurse ...")', timeout=45,
        limits="the walk skips AppData and node_modules and stops after 25s (says partial)")
async def find_file(ctx: Ctx, name: str, where: str = "home", newer_than_days: float = 0, **_) -> str:
    root = _path(where or "home")
    if not root.exists():
        raise Fail("NOT_FOUND", f"{root} doesn't exist", f'find_file("{name}")')
    hits: list = []
    in_temp = str(root).lower().startswith(os.environ.get("TEMP", "~~").lower())
    if not in_temp:  # the Search index doesn't cover %TEMP%
        like = name.replace("'", "''").replace("*", "%").replace("?", "_")
        like = like if "%" in like else f"%{like}%"
        scope = str(root).replace("\\", "/").replace("'", "''")
        when = f" AND System.DateModified >= '{(dt.datetime.now() - dt.timedelta(days=float(newer_than_days))):%Y-%m-%d}'" if newer_than_days else ""
        sql = (f"SELECT TOP 20 System.ItemPathDisplay, System.DateModified FROM SYSTEMINDEX WHERE System.FileName LIKE '{like}' "
               f"AND SCOPE='file:{scope}'{when} ORDER BY System.DateModified DESC")
        cmd = ("$c = New-Object -ComObject ADODB.Connection; $r = New-Object -ComObject ADODB.Recordset; "
               "$c.Open(\"Provider=Search.CollatorDSO;Extended Properties='Application=Windows';\"); "
               f"$r.Open({_q(sql)}, $c); while (-not $r.EOF) {{ $r.Fields.Item('System.ItemPathDisplay').Value; $r.MoveNext() }}; $r.Close(); $c.Close()")
        try:
            out, code = await ps(ctx, cmd, timeout=15)
            hits = [Path(l.strip()) for l in out.splitlines() if re.match(r"^[A-Za-z]:\\", l.strip())]
        except Exception:
            hits = []
    via, partial = "search index", False
    if not hits:
        found, partial = await asyncio.to_thread(_walk_find_sync, root, name, float(newer_than_days or 0))
        found.sort(key=lambda x: -x[1])
        hits, via = [p for p, _ in found[:20]], "folder walk"
    hits = [p for p in hits if not _private(p)]
    if not hits:
        raise Fail("NOT_FOUND", f"no file like {name!r} under {root}" + (" (partial: the walk hit its time limit)" if partial else ""),
                   f'find_file("{name}", where="C:\\\\")' if str(root) != "C:\\" else "ask_user")
    lines = [f"{p}  {_size(p)}  {dt.datetime.fromtimestamp(p.stat().st_mtime):%Y-%m-%d %H:%M}" if p.exists() else str(p) for p in hits]
    return ok(f"{len(hits)} match{'es' if len(hits) > 1 else ''} for {name!r}{' (partial)' if partial else ''}:\n" + "\n".join(lines), via=via)


def _xml_text(data: bytes) -> str:
    text = re.sub(rb"</w:p>|</a:p>|<w:br/>|</row>", b"\n", data)
    text = re.sub(rb"<w:tab/>|</c>", b"\t", text)
    text = re.sub(rb"<[^>]+>", b"", text)
    import html
    return html.unescape(text.decode("utf-8", "replace"))


OFFICE = (".docx", ".xlsx", ".pptx")  # zips whose text is extracted: no line tags, no fingerprint, never edited as text


def _decode(p: Path, data: bytes) -> tuple[str, str]:
    """A text file's bytes -> (text, how to write it back): the BOM, byte order and code page it had, so an edit by line
    (edit_lines) doesn't re-encode the rest of the file."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace"), "utf-16-bom-le" if data[0] == 0xFF else "utf-16-bom-be"
    if b"\x00" in data[:4096]:
        if data[1:4096:2].count(0) > len(data[1:4096:2]) * 0.4:  # BOM-less UTF-16 (PowerShell's > writes it)
            return data.decode("utf-16-le", "replace"), "utf-16-le"
        raise Fail("UNSUPPORTED", f"{p.name} is a binary file", f'file_op("info", "{p}")')
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace"), "cp1252"


def _encode(text: str, how: str) -> bytes:
    """_decode's inverse (UnicodeEncodeError when the new text has characters a cp1252 file can't hold)."""
    if how == "utf-16-bom-le":
        return b"\xff\xfe" + text.encode("utf-16-le")
    if how == "utf-16-bom-be":
        return b"\xfe\xff" + text.encode("utf-16-be")
    return text.encode(how)


def _file_text_sync(p: Path) -> str:
    return _file_text_print_sync(p)[0]


def _file_text_print_sync(p: Path) -> tuple[str, tuple | None]:
    """(the file's text, the fingerprint of exactly the bytes it came from; None for docx/xlsx/pptx)."""
    ext = p.suffix.lower()
    st = p.stat()
    if st.st_size > 5_000_000 and ext not in OFFICE:
        raise Fail("UNSUPPORTED", f"{p.name} is {_size(p)}; IO reads files up to 5 MB whole",
                   f'read_file("{p}", tail=200) for its end, or find="words" for the lines about something')
    if ext in OFFICE:
        with zipfile.ZipFile(p) as z:
            names = z.namelist()
            if ext == ".docx":
                parts = ["word/document.xml"]
            elif ext == ".pptx":
                parts = sorted((n for n in names if re.match(r"ppt/slides/slide\d+\.xml", n)), key=lambda n: int(re.findall(r"\d+", n)[0]))
            else:
                parts = ["xl/sharedStrings.xml"] + sorted(n for n in names if n.startswith("xl/worksheets/sheet"))
            return "\n".join(_xml_text(z.read(n)) for n in parts if n in names), None
    if ext == ".pdf":
        raise Fail("UNSUPPORTED", "no PDF reader", f'open_path("{p}") then read_window(...)')
    data = p.read_bytes()
    return _decode(p, data)[0], _print_of(st, data)


# --- stale-edit protection: an edit is made from what the model read; if the user (or an editor, a build) changed the
# file since, an edit or rewrite from the old read silently undoes their change. Files never read in a task aren't held.

def _print_of(st: os.stat_result, data: bytes) -> tuple[int, int, str]:
    return len(data), st.st_mtime_ns, hashlib.sha1(data).hexdigest()


def _changed_since_read(ctx: Ctx, p: Path) -> bool:
    was = ctx.file_prints.get(str(p).lower())
    if not was:
        return False
    try:
        st = p.stat()
        if (st.st_size, st.st_mtime_ns) == tuple(was[:2]):
            return False
        return hashlib.sha1(p.read_bytes()).hexdigest() != was[2]  # touched but the same bytes is no change
    except OSError:
        return False


def _refuse_stale(ctx: Ctx, p: Path) -> None:
    """edit_file, and write_file over or onto an existing file: refused when this task read it and it changed since."""
    if p.is_file() and _changed_since_read(ctx, p):
        raise Fail("STALE", f"{p} changed on disk since this task read it (the user or another program edited it); "
                   "an edit from the old text would undo that change", f'read_file("{p}") again, then redo the edit')


def _note_written(ctx: Ctx, p: Path) -> None:
    """After IO's own write to a file it read: the new bytes are what the task knows, so its next edit isn't refused."""
    key = str(p).lower()
    if key in ctx.file_prints:
        try:
            st = p.stat()
            ctx.file_prints[key] = _print_of(st, p.read_bytes())
        except OSError:
            ctx.file_prints.pop(key, None)


# --- lines and anchors ("hashline"): read_file(anchors=true) shows each line as N:hh|text, hh a 2-character tag of the
# line's text; edit_lines names lines by number and proves it saw them by their tags, instead of copying old text exactly
# (small models slip on spaces and long blocks, and every copied character is generated again)

_EOL = re.compile(r"\r\n|\n|\r")
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"
_TAG_PREFIX = re.compile(r"^\d+:[0-9a-z]{2}\|")  # a line as read_file(anchors=true) showed it, pasted back into new_text


def _rows(text: str) -> list[tuple[str, str]]:
    """(line, its line break) pairs; a last line without a break gets "". The one numbering lines=, anchors, edit_lines
    and the outlines share (\\r\\n, \\n and a lone \\r each end a line, as in editors and Python's ast)."""
    out, pos = [], 0
    for m in _EOL.finditer(text):
        out.append((text[pos:m.start()], m.group()))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], ""))
    return out


def _hh(line: str) -> str:
    """A line's tag: crc32 of its text (no line break) as 2 base-36 characters. With the line number, a changed or moved
    line is caught 1295 times in 1296."""
    n = zlib.crc32(line.encode("utf-8", "replace")) % 1296
    return _B36[n // 36] + _B36[n % 36]


def _tagged(rows: list, lo: int, hi: int) -> list[str]:
    return [f"{n}:{_hh(rows[n - 1][0])}|{rows[n - 1][0]}" for n in range(lo, hi + 1)]


def _clip(s: str, width: int) -> str:
    return s if len(s) <= width else s[:builtins_max(1, width - 1)] + "…"


def _fit(lines: list[str], budget: int) -> tuple[str, int]:
    """As many whole lines as fit the budget (at least one, cut if it alone is too long): (text, lines kept)."""
    used, n = 0, 0
    for ln in lines:
        if n and used + len(ln) + 1 > budget:
            break
        used += len(ln) + 1
        n += 1
    text = "\n".join(lines[:n])
    return (text if len(text) <= budget else _clip(text, budget)), n


def _log_lines_sync(p: Path, shown: str, tail: int, find: str, budget: int) -> str:
    """The end of a file of any size (a log), or its lines with some words (in the last `tail` lines when given): read
    in pieces from the end, or line by line, never the whole file into memory."""
    size = p.stat().st_size
    if not find:
        n = builtins_max(1, min(int(tail), 5000))
        data, pos, chunk = b"", size, 65536
        with open(p, "rb") as f:
            while pos > 0 and data.count(b"\n") <= n:
                step = min(chunk, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + data
                chunk *= 2
        rows = data.decode("utf-8", "replace").replace("\r\n", "\n").split("\n")
        if rows and rows[-1] == "":
            rows.pop()
        rows = rows[-n:]
        body = "\n".join(rows)
        if len(body) > budget:  # the newest lines matter most in a log
            body = "[... older lines cut to fit]\n" + body[-budget:].split("\n", 1)[-1]
        return f"{shown} ({_size(p)}), its last {len(rows)} lines:\n{body}" if rows else f"{shown}: (empty file)"
    # "a|b" and "a, b" are alternatives (models write both, meaning any of them); words inside one must all be on the line
    alts = [[w.lower() for w in part.split()] for part in re.split(r"[|,]", find) if part.split()]
    hits: list[tuple[int, str]] = []
    total = 0
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        for total, line in enumerate(f, 1):
            low = line.lower()
            if any(all(w in low for w in alt) for alt in alts):
                hits.append((total, line.rstrip("\r\n")))
                if len(hits) > 20000:
                    del hits[:10000]  # only the newest are shown anyway
    if tail:
        hits = [h for h in hits if h[0] > total - tail]
    scope = f"in its last {tail:,} lines" if tail else f"of {total:,}"
    if not hits:
        return f"{shown} ({_size(p)}): no line {scope} has " + " or ".join(" and ".join(a) for a in alts)
    shown_rows: list[str] = []
    used = 0
    for i, line in reversed(hits):  # newest first until the budget is spent, then back in file order
        row = f"{i}: {_clip(line, 600)}"
        if used + len(row) > budget and shown_rows:
            break
        shown_rows.append(row)
        used += len(row) + 1
    shown_rows.reverse()
    more = f" (the newest {len(shown_rows)} shown)" if len(shown_rows) < len(hits) else ""
    return f"{shown} ({_size(p)}): {len(hits)} lines {scope} with \"{find}\"{more}:\n" + "\n".join(shown_rows)


def _line_span(spec: str, total: int, shown: str) -> tuple[int, int]:
    """lines="120-180" -> (120, 180), clamped to the file. "120" is that line, "120-" runs to the end."""
    m = re.fullmatch(r"\s*L?(\d+)\s*(?:(-|–|—|:|\.\.|to|,)\s*L?(\d*))?\s*", str(spec), re.I)
    if not m:
        raise Fail("BAD_ARGS", f"lines={str(spec)[:30]!r}: give one range like 120-180", f'read_file("{shown}", lines="1-200")')
    a = max(1, int(m.group(1)))
    b = int(m.group(3)) if m.group(3) else (total if m.group(2) else a)
    if a > total:
        raise Fail("BAD_ARGS", f"the file has {total} lines", f'read_file("{shown}", lines="{max(1, total - 99)}-{total}")')
    if b < a:
        raise Fail("BAD_ARGS", f"lines={spec}: the end is before the start", f'read_file("{shown}", lines="{a}-{a + 60}")')
    return a, min(b, total)


# --- artifact:// links: boss saves a tool result too long for the model's context whole to data/artifacts/<id>.txt and
# says "full output: artifact://<id>"; read_file reads it like a file (lines=, find=). IO's own data, so exempt from the
# user-folders rule, and read-only: the id can never name anything else.

ARTIFACTS = HERE / "data" / "artifacts"
_ARTIFACT_ID = re.compile(r"[A-Za-z0-9-]{1,80}")
_ARTIFACT_LINK = re.compile(r"(?i)^\s*[\"']?artifact://(.*?)[\"']?\s*$")


def artifact_path(id: str) -> Path:
    """data/artifacts/<id>.txt. ValueError for an id that isn't letters, digits and dashes (no slashes or dots, so it can
    never point outside the folder). The writer (boss) makes the folder."""
    s = str(id or "").strip()
    if not _ARTIFACT_ID.fullmatch(s):
        raise ValueError(f"bad artifact id {s[:40]!r}")
    return ARTIFACTS / f"{s}.txt"


def new_artifact_id() -> str:
    """A fresh id for artifact_path: time first (names sort by age), then 6 random hex digits."""
    return time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()


def _artifact_of(path: str) -> Path | None:
    """artifact://<id> -> its file; None for any other path."""
    m = _ARTIFACT_LINK.match(str(path or ""))
    if not m:
        return None
    try:
        return artifact_path(m.group(1).strip())
    except ValueError:
        raise Fail("BAD_ARGS", f"artifact://{m.group(1)[:40]} isn't a valid link (the id is letters, digits and dashes only)",
                   "the artifact:// link exactly as the result gave it") from None


# --- outlines: a big source file read from the top costs the model's whole context for the first few hundred lines,
# usually not the part it needs. The outline (a few KB) says what is where; lines= then reads just that part.

CODE_EXTS = {".py": "py", ".js": "js", ".ts": "js", ".tsx": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js", ".java": "c", ".cs": "c",
             ".go": "go", ".rs": "rs", ".c": "c", ".cpp": "c", ".h": "c", ".hpp": "c", ".rb": "rb", ".php": "php", ".swift": "swift",
             ".kt": "kt"}
OUTLINE_LINES, OUTLINE_BYTES, OUTLINE_CHARS = 250, 12_000, 6000  # outline a code file above 250 lines or 12 KB, in <= 6000 chars
_CONST = re.compile(r"_?[A-Z][A-Z0-9_]*")


def _squeeze(s: str, width: int) -> str:
    return _clip(" ".join(str(s).split()), width)


def _first_line(doc: str | None, width: int = 80) -> str:
    return next((_squeeze(ln, width) for ln in (doc or "").splitlines() if ln.strip()), "")


def _packed(items: list[str], width: int) -> str:
    out, used = [], 0
    for i, s in enumerate(items):
        if out and used + len(s) + 2 > width:
            return ", ".join(out) + f" (+{len(items) - i} more)"
        out.append(s)
        used += len(s) + 2
    return ", ".join(out)


def _py_entry(n: ast.AST, level: int, out: list) -> None:
    """A def or class (with its methods, and a nested class's methods) as (start, end, level, head, doc, name)."""
    start = min([d.lineno for d in n.decorator_list] + [n.lineno])  # a decorator belongs to what it decorates
    deco = "".join("@" + _squeeze(ast.unparse(d.func if isinstance(d, ast.Call) else d), 30) + " " for d in n.decorator_list)
    doc = _first_line(ast.get_docstring(n))
    if isinstance(n, ast.ClassDef):
        bases = ", ".join(ast.unparse(b) for b in n.bases + n.keywords)
        out.append((start, n.end_lineno, level, f"{deco}class {n.name}" + (f"({_squeeze(bases, 60)})" if bases else ""), doc, n.name))
        if level < 2:
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    _py_entry(m, level + 1, out)
        return
    ret = f" -> {_squeeze(ast.unparse(n.returns), 30)}" if n.returns else ""
    kw = "async def" if isinstance(n, ast.AsyncFunctionDef) else "def"
    out.append((start, n.end_lineno, level, f"{deco}{kw} {n.name}({_squeeze(ast.unparse(n.args), 90)}){ret}", doc, n.name))


def _outline_py(text: str) -> tuple[list, list] | None:
    """Python by ast: (header lines: what it is, imports, constants; entries). None when it doesn't parse."""
    try:
        with warnings.catch_warnings():  # "\d" in a plain string is a SyntaxWarning, not this file's problem
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    entries: list = []
    mods, spans, consts = [], [], []
    blocks = tuple(t for t in (ast.If, ast.Try, getattr(ast, "TryStar", None)) if t)

    def flat(body: list):  # module level, and under a module-level if/try (optional imports, per-platform defs)
        for n in body:
            if isinstance(n, ast.If) and "__name__" in ast.unparse(n.test):
                entries.append((n.lineno, n.end_lineno, 0, "if __name__ == '__main__':", "", "__main__"))
            elif isinstance(n, blocks):
                yield from flat(n.body + n.orelse + getattr(n, "finalbody", []) + [s for h in getattr(n, "handlers", []) for s in h.body])
            else:
                yield n

    for n in flat(tree.body):
        if isinstance(n, ast.Import):
            mods += [a.name for a in n.names]
            spans.append(n.lineno)
        elif isinstance(n, ast.ImportFrom):
            mods.append("." * n.level + (n.module or ""))
            spans.append(n.lineno)
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            consts += [f"{t.id} {n.lineno}" + (f"-{n.end_lineno}" if n.end_lineno > n.lineno else "")
                       for t in targets if isinstance(t, ast.Name) and _CONST.fullmatch(t.id)]
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _py_entry(n, 0, entries)
    head = []
    about = _first_line(ast.get_docstring(tree), 110)
    if about:
        head.append(f"about: {about}")
    if mods:
        head.append(f"imports {min(spans)}-{max(spans)}: " + _packed(list(dict.fromkeys(mods)), 300))
    if consts:
        head.append("constants: " + _packed(consts, 450))
    return head, entries


# the other languages: a regex outline on the code with comments and string contents taken out (a brace in a string
# must not end a function), each declaration's end found by its braces (by indentation in Ruby)
# a string ends on its own line or isn't one (an apostrophe in JSX text, a Rust lifetime 'a): the quote is then a character
_LEX = {
    "c": re.compile(r"//|/\*|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'"),
    "js": re.compile(r"//|/\*|`|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|/|\{|\}"),  # / : maybe a /regex/; { } : ${ } nesting
    "go": re.compile(r"//|/\*|`|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'"),
    "rs": re.compile(r"//|/\*|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])'"),
    "php": re.compile(r"//|/\*|#(?!\[)|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'"),
}
_LEX["swift"], _LEX["kt"] = _LEX["c"], _LEX["c"]
_TICK_TEXT = re.compile(r"(?:\\.|[^`\\$]|\$(?!\{))*")  # a template's text up to its closing ` or a ${
_REGEX_END = re.compile(r"(?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\\\[])+/[a-z]*")  # a JS regex literal's body after its first /
_REGEX_BEFORE = re.compile(r"(?:^|[(,=:\[!&|?{};+\-~^]|\b(?:return|typeof|case|do|else|in|of|void|yield|await))$")
_KW = frozenset("if for while switch catch return else do try finally new delete sizeof throw case using lock foreach with typeof "
                "await yield when match elif unless until synchronized fixed checked unchecked function super this defined assert "
                "loop select go defer import package require include not and or in is goto".split())
_MODS = (r"(?:(?:public|private|protected|internal|static|abstract|sealed|partial|readonly|unsafe|final|virtual|override|extern|"
         r"inline|async|new|file|synchronized|native|default|const|constexpr|explicit|friend|volatile|transient|strictfp|open|data|"
         r"enum|annotation|inner|value|companion|lateinit|suspend|operator|infix|tailrec|external|actual|expect|fileprivate|"
         r"mutating|nonmutating|convenience|required|dynamic|lazy|weak|unowned|indirect|declare|export|pub(?:\([^)]*\))?)\s+)*")
_OPEN_CLASS = r"(?:<.*>)?\s*(?:$|[:{(,]|extends\b|implements\b|where\b|final\b|sealed\b|permits\b)"
# (kind, pattern, where, needs a { } body, is a c-style "type name(" line that must look like a declaration)
# where: any depth; top = depth 0; member = depth 0 or directly inside a class/namespace/impl; inbox = directly inside one;
# deep_body = like member, or deeper with a body of several lines (a one-line arrow inside a function is a local, noise)
_OUTLINE_RX = {k: [(kind, re.compile(rx), where, body, decl) for kind, rx, where, body, decl in v] for k, v in {
    "js": [
        ("box", r"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?class\b", "any", True, False),
        ("box", r"^\s*(?:export\s+)?(?:declare\s+)?(?:interface|namespace|enum)\s+[\w$.]+", "any", True, False),
        ("def", r"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:async\s+)?function\b", "any", False, False),
        ("def", r"^\s*(?:export\s+)?(?:const|let|var)\s+[\w$]+\s*(?::[^=]+)?=\s*(?:async\s+)?"
                r"(?:function\b|(?:\([^()]*\)|[\w$]+)\s*(?::[^=]+?)?=>|\(\s*$)", "deep_body", False, False),
        ("def", r"^\s*(?:(?:module\.)?exports\.[\w$]+|[\w$.]+\.prototype\.[\w$]+)\s*=\s*(?:async\s+)?(?:function\b|\([^()]*\)\s*=>)", "top", False, False),
        ("def", r"^\s*(?:(?:static|async|get|set|public|private|protected|readonly|override|abstract|declare)\s+)*\*?\s*#?[\w$]+\s*"
                r"(?:<[^>]*>)?\s*\(", "inbox", True, True),
        ("def", r"^\s*(?:(?:static|public|private|protected|readonly)\s+)*#?[\w$]+\s*(?::[^=]+)?=\s*(?:async\s+)?(?:\([^()]*\)|[\w$]+)\s*=>",
         "inbox", False, False),
        ("export", r"^\s*(?:export\s+(?:default\b|\*|\{|(?:const|let|var|type)\s)|module\.exports\s*=)", "top", False, False),
        ("type", r"^\s*type\s+[\w$]+\s*(?:<[^>]*>)?\s*=", "top", False, False),
    ],
    "c": [
        ("box", r"^\s*(?:\[[^\]]*\]\s*)*(?:template\s*<.*>\s*)?(?:typedef\s+)?" + _MODS + r"(?:class|interface|struct|enum(?:\s+class)?|"
                r"record(?:\s+(?:class|struct))?|namespace|union|@interface)\s+[\w.:]+" + _OPEN_CLASS, "member", True, False),
        ("box", r"^\s*extern\s+\"\"\s*\{", "top", True, False),
        ("def", r"^\s*(?:\[[^\]]*\]\s*)*(?:template\s*<.*>\s*)?(?:[\w$:<>,\[\]*&~.?]+\s+)+[*&]*\s*"
                r"(?:operator\s*[^\s(]+|~?[A-Za-z_]\w*(?:::~?[A-Za-z_]\w*)*)\s*(?:<[^>()]*>)?\s*\(", "member", True, True),
        ("def", r"^\s*~?[A-Za-z_]\w*(?:<[^>]*>)?::~?[A-Za-z_]\w*\s*\(", "member", True, True),  # C++ Foo::Foo(...) out of line
        ("def", r"^[A-Za-z_]\w*\s*\(", "top", True, True),  # C with the return type on the line before (GNU style)
    ],
    "go": [
        ("def", r"^func\b", "top", False, False),
        ("type", r"^type\s+\w+", "top", False, False),
    ],
    "rs": [
        ("def", r"^\s*" + _MODS + r"(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+(?:\"\"\s+)?)?fn\s+\w+", "member", False, False),
        ("box", r"^\s*" + _MODS + r"(?:unsafe\s+)?(?:impl|trait|mod)\b", "member", True, False),
        ("type", r"^\s*" + _MODS + r"(?:struct|enum|union|type)\s+\w+", "member", False, False),
        ("def", r"^\s*macro_rules!\s*\w+", "top", False, False),
    ],
    "php": [
        ("box", r"^\s*" + _MODS + r"(?:class|interface|trait|enum)\s+\w+", "any", True, False),
        ("def", r"^\s*" + _MODS + r"function\s+&?\w+", "any", False, False),
    ],
    "swift": [
        ("def", r"^\s*(?:@\w+\s+)*" + _MODS + r"(?:class\s+)?(?:func\s+\S+?\s*[(<]|init[?!]?\s*[(<]|deinit\b|subscript\s*\()", "member", False, False),
        ("box", r"^\s*(?:@\w+\s+)*" + _MODS + r"(?:class|struct|enum|protocol|extension|actor)\s+\w+", "member", True, False),
    ],
    "kt": [
        ("def", r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*" + _MODS + r"fun\b", "member", False, False),
        ("box", r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*" + _MODS + r"(?:class|interface|object)\b", "member", False, False),
    ],
    "rb": [
        ("box", r"^\s*(?:class|module)\s+[\w:]+", "any", False, False),
        ("def", r"^\s*def\s+", "any", False, False),
    ],
    "py": [  # only when ast can't parse the file (Python 2, a half-written edit)
        ("box", r"^\s*class\s+\w+", "any", False, False),
        ("def", r"^\s*(?:async\s+)?def\s+\w+", "any", False, False),
    ],
}.items()}
_BRACE = re.compile(r"[{};]")


def _code_only(lines: list[str], lang: str) -> list[str]:
    """Each line without its comments and with its strings emptied. Block comments and `templates` span lines; a
    template's ${...} is code again (with its own strings and `nested ${templates}`), so its braces stay balanced."""
    lex = _LEX[lang]
    out, in_block, in_tick = [], False, False
    held: list[int] = []  # JS: for each ${ open inside a template, the { } depth within it
    for line in lines:
        buf, pos, n = [], 0, len(line)
        while pos < n:
            if in_block:
                j = line.find("*/", pos)
                if j < 0:
                    break
                in_block, pos = False, j + 2
                continue
            if in_tick:  # a template's text: up to its closing ` or a ${
                pos = _TICK_TEXT.match(line, pos).end()
                if pos >= n:
                    break
                if line[pos] == "\\":  # a backslash at the very end of the line
                    pos += 2
                elif line[pos] == "`":
                    in_tick, pos = False, pos + 1
                    buf.append("`")
                else:
                    in_tick, pos = False, pos + 2
                    held.append(0)
                continue
            m = lex.search(line, pos)
            if not m:
                buf.append(line[pos:])
                break
            buf.append(line[pos:m.start()])
            t, pos = m.group(), m.end()
            if t in ("//", "#"):
                break
            if t == "/":  # JS: a /regex/ (its [({] must not count) where an operand can start, else division
                r = _REGEX_END.match(line, pos) if _REGEX_BEFORE.search("".join(buf).rstrip()) else None
                buf.append('""' if r else "/")
                pos = r.end() if r else pos
            elif t == "/*":
                in_block = True
            elif t == "`":
                in_tick = True
                buf.append("`")
            elif t == "{":
                if held:
                    held[-1] += 1
                buf.append(t)
            elif t == "}":
                if held and held[-1] == 0:  # the } of a ${: back in the template's text
                    held.pop()
                    in_tick = True
                    continue
                if held:
                    held[-1] -= 1
                buf.append(t)
            else:
                buf.append(t if len(t) == 1 else t[0] * 2)
        out.append("".join(buf))
    return out


def _looks_decl(code: str) -> bool:
    """A c-style 'type name(' line that declares rather than calls: no keyword first or right before the (, no
    assignment before it, not obj.method(."""
    paren = code.find("(")
    pre = code[:paren] if paren >= 0 else code
    words = re.findall(r"[~\w$#]+", pre)
    if not words or words[0] in _KW or words[-1] in _KW:
        return False
    if re.search(r"(?<![=!<>])=(?![=>])", pre) and "operator" not in pre:
        return False
    return not re.search(r"\.\s*[~\w$#]+\s*(?:<[^>]*>)?\s*$", pre)


def _brace_end(code: list[str], i: int, stop: int) -> tuple[int, bool]:
    """(the last line of the declaration starting at line i, whether it has a { } body). A body must open within a few
    lines and before the next declaration (stop), else it's a one-liner (Kotlin's fun f() = x, a prototype)."""
    d, opened = 0, False
    for j in range(i, len(code)):
        if not opened and j > i and (j >= stop or j - i > 12):
            return i, False
        for m in _BRACE.finditer(code[j]):
            ch = m.group()
            if ch == "{":
                d += 1
                opened = True
            elif ch == "}":
                d -= 1
                if d < 0:  # an enclosing block closed too
                    return (j, True) if opened else (i, False)
            elif not opened and d == 0:  # a ; before any body
                return j, False
        if opened and d <= 0:  # judged at the line's end: f({ a }) { opens its body after closing a destructuring
            return j, True
    return (len(code) - 1, True) if opened else (i, False)


def _indent_end(lines: list[str], i: int, ruby: bool) -> int:
    """By indentation: the last line before one indented no deeper (Ruby: that line itself, when it is the end)."""
    ind = len(lines[i].expandtabs(4)) - len(lines[i].expandtabs(4).lstrip())
    last = i
    for j in range(i + 1, len(lines)):
        s = lines[j].strip()
        if not s or s.startswith("#"):
            continue
        if len(lines[j].expandtabs(4)) - len(lines[j].expandtabs(4).lstrip()) <= ind and not s.startswith((")", "]", "}")):
            return j if ruby and re.match(r"end\b", s) else last
        last = j
    return last


def _decl_name(code: str, kind: str) -> str:
    """The declared name, for the packed names-only outline: the word before the first ( that isn't a keyword (Go's
    func (r *T) Name( is Name), else the word after class/const/def/..."""
    if kind == "def":
        for m in re.finditer(r"([~\w$#.:]+)\s*(?:<[^>()]*>)?\s*\(", code):
            if m.group(1) not in _KW and m.group(1) not in ("func", "fun", "fn", "def"):
                return m.group(1)
    m = re.search(r"\b(?:class|interface|struct|enum|record|namespace|union|trait|impl|mod|module|object|protocol|extension|actor|"
                  r"type|const|let|var|def|fn|fun|func|function)\s+([\w$.:<>]+)", code)
    return m.group(1) if m else ""


def _outline_re(lines: list[str], lang: str, ext: str) -> list:
    code = lines if lang in ("rb", "py") else _code_only(lines, lang)
    depth, d = [], 0
    for c in code:
        depth.append(d)
        d = max(0, d + c.count("{") - c.count("}"))
    cands = []
    for i, c in enumerate(code):
        if c.strip():
            hit = next((x for x in _OUTLINE_RX[lang] if x[1].match(c)), None)
            if hit:
                cands.append((i, *hit))
    boxes, entries = [], []
    for k, (i, kind, _rx, where, body, decl) in enumerate(cands):
        while boxes and boxes[-1][1] < i:
            boxes.pop()
        direct = bool(boxes) and depth[i] == boxes[-1][2] + 1
        shallow = depth[i] == 0 or direct or lang in ("rb", "py")
        if lang not in ("rb", "py") and ((where == "top" and depth[i] != 0) or (where == "member" and not shallow)
                                         or (where == "inbox" and not direct)):
            continue
        if decl and not _looks_decl(code[i]):
            continue
        if lang in ("rb", "py"):
            end, opened = _indent_end(lines, i, lang == "rb"), True
        else:
            end, opened = _brace_end(code, i, cands[k + 1][0] if k + 1 < len(cands) else len(code))
        # a c-style "type name(...)" without a body is a call or a variable, except a header's prototype or C#'s => body
        if body and not opened and not (decl and (ext in (".h", ".hpp") or "=>" in code[i])):
            continue
        if where == "deep_body" and not shallow and not (opened and end > i):
            continue
        if kind == "box" and opened:
            boxes.append((i, end, depth[i]))
        head = _squeeze(re.sub(r"\s*\{\s*$", "", lines[i].strip()), 110)
        entries.append([i + 1, end + 1, 0, head, "", _decl_name(code[i], kind) or head[:24]])
    stack: list = []
    for e in entries:  # nesting by line ranges
        while stack and stack[-1] < e[0]:
            stack.pop()
        e[2] = len(stack)
        stack.append(e[1])
    return [tuple(e) for e in entries]


def _outline_text(text: str, rows: list, lang: str, ext: str, shown: str, info: str, cap: int) -> str:
    """The outline of a big code file in <= cap characters: header (Python: what it is, imports, constants), then each
    class / function / method as 'start-end head  # first docstring line', nested by indentation. '' if none found."""
    got = _outline_py(text) if lang == "py" else None
    head, entries = got if got else ([], _outline_re([r[0] for r in rows], lang, ext))
    if not entries:
        return ""
    entries = sorted(entries, key=lambda e: (e[0], e[2]))
    ex = next((f"{s}-{e}" for s, e, *_ in entries if e > s), f"1-{min(len(rows), 200)}")
    top = f"{shown} ({info}): its outline, not its text"
    foot = (f'[an outline: read_file("{shown}", lines="{ex}") reads a part (anchors=true too, to change it with edit_lines); '
            f'find= searches; raw=true reads from the top]')
    span = []  # how many entries follow inside each one
    for k, e in enumerate(entries):
        j = k + 1
        while j < len(entries) and entries[j][0] <= e[1]:
            j += 1
        span.append(j - k - 1)

    def line(k: int, docs: bool, width: int, deepest: int) -> str:
        s, e, lv, h, doc, _n = entries[k]
        hidden = sum(1 for x in entries[k + 1:k + 1 + span[k]] if x[2] > deepest)
        return (f"{'  ' * lv}{s}-{e} " if e > s else f"{'  ' * lv}{s} ") + _clip(h, width) + (f"  # {doc}" if docs and doc else "") + \
            (f"  [{hidden} more inside]" if hidden else "")

    # shorter lines first; fewer levels only while that still names a fair share of the file (one big class with 125
    # methods must not shrink to one line)
    stages = [(True, 160, 9), (False, 160, 9), (False, 70, 9)] + \
        [(False, 70, lv) for lv in (1, 0) if sum(1 for e in entries if e[2] <= lv) >= min(15, len(entries))]
    for docs, width, deepest in stages:
        out = "\n".join([top, *head, *(line(k, docs, width, deepest) for k, e in enumerate(entries) if e[2] <= deepest), foot])
        if len(out) <= cap:
            return out
    # still too long (thousands of lines): names only, packed: every level, then the top level; names matter more than
    # the imports and constants, and the public names more than the _private helpers; cut only when none of that fits
    about = [h for h in head if h.startswith("about:")]
    tries, seen = [], []
    for deepest, label in ((9, "all"), (0, "top level")):
        items = [(f"{s}-{e} {n}" if e > s else f"{s} {n}", n) for s, e, lv, _h, _d, n in entries if lv <= deepest]
        if items in seen:
            continue
        seen.append(items)
        public = [t for t in items if not t[1].startswith("_")]
        tries += [(head, items, f"{label} (start-end name): "), (about, items, f"{label} (start-end name): ")]
        if 0 < len(public) < len(items):
            tries.append((about, public, f"{label}, the {len(items) - len(public)} _private ones left out (start-end name): "))
    out = ""
    for hd, items, label in tries:
        room = cap - len(top) - len(foot) - sum(len(h) + 1 for h in hd) - len(label) - 4
        out = "\n".join([top, *hd, label + _packed([t[0] for t in items], builtins_max(100, room - 16)), foot])
        if sum(len(t[0]) + 2 for t in items) <= room and len(out) <= cap:
            return out
    return out[:cap]


@action("read_file", group="FILE", summary="a file's text (txt, csv, json, docx, xlsx...); big code: an outline",
        params="""
        path s the file's path (or an artifact:// link)
        find s? only the lines about these words
        lines s? only these lines, like 120-180
        tail i? only the last N lines (any size: logs)
        anchors b? prefix lines N:hh| for edit_lines
        max i? most characters (default: all that fits)
        raw b? full text even for a big code file
        """, cost=0.1, star=True, top="path,find?", fallback='FileSystem(mode="read", path)', hide=("raw",),
        limits="no PDFs; up to 5 MB whole (tail= or find= read any size); never opens an editor; a big code file gives its outline")
async def read_file(ctx: Ctx, path: str, find: str = "", max: int = 0, lines: str = "", anchors: bool = False, raw: bool = False,
                    tail: int = 0, **_) -> str:
    art = _artifact_of(path)
    if art is not None:  # a saved tool result: IO's own data, so no _private refusal and no user-folder rule
        p, shown = art, f"artifact://{art.stem}"
        if not p.is_file():
            raise Fail("NOT_FOUND", f"{shown} doesn't exist (saved results are kept about a week)", "run the tool that made it again")
    else:
        p = _path(path)
        shown = str(p)
        if _private(p):
            raise Fail("BLOCKED", "that is IO's own data (keys and history), never read for a task", "ask_user")
        if not p.exists():
            raise Fail("NOT_FOUND", f"{p} doesn't exist", f'find_file("{p.name}")')
        if p.is_dir():
            raise Fail("BAD_ARGS", f"{p} is a folder", f'list_files("{p}")')
    if (tail and not lines) or (find and not lines and p.suffix.lower() not in OFFICE and p.stat().st_size > 5_000_000):
        room = builtins_max(4000, ctx.page_chars or 4000)
        budget = builtins_max(200, min(int(max or room), builtins_max(20000, room)))
        return ok(await asyncio.to_thread(_log_lines_sync, p, shown, int(tail or 0), find, budget))
    text, fp = await asyncio.to_thread(_file_text_print_sync, p)
    if fp and art is None:  # what this task now knows of the file: edits refuse if it changes on disk after this
        ctx.file_prints[str(p).lower()] = fp
    # as much as the brain's context allows (a large-context brain reads a whole source file in one call, not 4K at a time)
    room = builtins_max(4000, ctx.page_chars or 4000)
    budget = builtins_max(200, min(int(max or room), builtins_max(20000, room)))
    head = f"{shown} ({_size(p)})"
    if not text.strip():
        return ok(f"{head}:\n(empty file)")
    rows = _rows(text)
    total = len(rows)
    tag = bool(anchors) and fp is not None  # tags only on plain text: docx/xlsx/pptx text isn't the file's lines
    note = " (anchors only on plain text files)" if anchors and not tag else ""
    if lines:
        a, b = _line_span(lines, total, shown)
        part = _tagged(rows, a, b) if tag else [r[0] for r in rows[a - 1:b]]
        if find:
            return ok(f"{head}, lines {a}-{b} of {total}{note}:\n" + _h().find_in_text("\n".join(part), find, budget=budget))
        body, kept = _fit(part, budget)
        more = (f'\n[stopped at line {a + kept - 1} to fit; next: read_file("{shown}", lines="{a + kept}-{b}"'
                f'{", anchors=true" if tag else ""})]') if kept < len(part) else ""
        return ok(f"{head}, lines {a}-{b} of {total}{note}:\n{body}{more}")
    if find:
        return ok(f"{head}{note}:\n" + _h().find_in_text("\n".join(_tagged(rows, 1, total)) if tag else text, find, budget=budget))
    lang = CODE_EXTS.get(p.suffix.lower())
    if lang and not raw and fp is not None and total >= 20 and (total > OUTLINE_LINES or fp[0] > OUTLINE_BYTES):
        out = await asyncio.to_thread(_outline_text, text, rows, lang, p.suffix.lower(), shown, f"{_size(p)}, {total} lines",
                                      min(budget, OUTLINE_CHARS))
        if out:
            return ok(out)
    if tag:
        body, kept = _fit(_tagged(rows, 1, total), budget)
        more = (f'\n[stopped at line {kept} to fit; next: read_file("{shown}", lines="{kept + 1}-{min(total, kept + 300)}", '
                f'anchors=true)]') if kept < total else ""
        return ok(f"{head}, {total} lines:\n{body}{more}")
    if len(text) <= budget:
        return ok(f"{head}{note}:\n{text}")
    cut = text.rfind("\n", 0, budget)
    whole = cut > budget * 0.8  # end on a whole line when one ends near the budget
    cut = cut if whole else budget
    nxt = text.count("\n", 0, cut) + (2 if whole else 1)  # the first line not shown in full
    return ok(f"{head}{note}:\n{text[:cut].rstrip(chr(13))}\n[{len(text) - cut} more characters, {total} lines in all; use find=, "
              f'or lines="{nxt}-{min(total, nxt + 199)}"]')


@action("write_file", group="FILE", summary="write text to a file (new by default; append or overwrite on request)",
        params="""
        path s the file's path
        text s what to write
        mode s? new|append|overwrite
        """, cost=0.1, star=True, top="path,text", bang=True, fallback='FileSystem(mode="write", path, content)',
        # a file this task wrote itself is its own work in progress (a test file being fixed asked twice in one run,
        # and an unattended run would wait on that forever); anything else that exists is the user's and asks first
        risky=lambda a, c: f"overwrite the file {a.get('path')}" if a.get("mode") == "overwrite" and _path(str(a.get("path", ""))).exists()
        and str(_path(str(a.get("path", "")))).lower() not in c.written else "",
        limits="only under the user's folders or %TEMP%; new never replaces a file")
async def write_file(ctx: Ctx, path: str, text: str, mode: str = "new", **_) -> str:
    p = _path(path)
    writable(ctx, p)
    if p.is_dir():
        raise Fail("BAD_ARGS", f"{p} is a folder", f'write_file("{p}\\\\notes.txt", ...)')
    if mode == "new" and p.exists():
        raise Fail("BLOCKED", f"{p} exists ({_size(p)})", f'write_file("{path}", ..., mode="append") or mode="overwrite"')
    await asyncio.to_thread(_refuse_stale, ctx, p)  # over or onto a file read earlier in this task that changed since

    def write() -> str:
        p.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            with open(p, "a", encoding="utf-8", newline="") as f:
                f.write(text)
        else:
            p.write_text(text, encoding="utf-8", newline="")
        return p.read_text(encoding="utf-8", errors="replace")

    try:
        back = await asyncio.to_thread(write)
    except PermissionError:
        raise Fail("BLOCKED", f"{p} is in use or read-only", "close the app using it, or another path")
    if text not in back:
        raise Fail("NO_CHANGE", f"wrote {p} but reading it back doesn't show the text", f'read_file("{p}")')
    if mode != "append":  # created here, or a rewrite the user allowed: later rewrites and edits of it don't ask again
        ctx.written.add(str(p).lower())
    await asyncio.to_thread(_note_written, ctx, p)  # IO's own change: its next edit of the file isn't STALE
    return ok(f"{'appended to' if mode == 'append' else 'wrote'} {p} ({_size(p)})")


@action("edit_file", group="FILE", summary="change part of a text file: replace exact old text with new (fix code without rewriting it)",
        params="""
        path s the file's path
        old s the exact text to replace, copied with its spaces
        new s what goes there instead
        all b? replace every match (default: old must match once)
        """, cost=0.1, star=True, top="path,old,new", bang=True, fallback='write_file(path, text, mode="overwrite")',
        risky=lambda a, c: "" if str(_path(str(a.get("path", "")))).lower() in c.written else f"edit the file {a.get('path')}",
        limits="text files; old must be in the file exactly; a file this task made or was allowed to change doesn't ask")
async def edit_file(ctx: Ctx, path: str, old: str, new: str, all: bool = False, **_) -> str:
    """Claude Code's Edit: whole-file rewrites to fix one line were slow (every character generated again) and each one
    risked new slips (a stray line, a mangled date) in the parts that were fine."""
    p = _path(path)
    writable(ctx, p)
    if not p.is_file():
        raise Fail("NOT_FOUND", f"{p} isn't a file", f'find_file("{p.name}")')
    if not old:
        raise Fail("BAD_ARGS", "old is empty", 'write_file(path, text, mode="append") to add to the end')
    await asyncio.to_thread(_refuse_stale, ctx, p)

    def edit() -> tuple[int, str]:
        with open(p, encoding="utf-8", newline="") as f:  # newline="": keep the file's own line endings
            text = f.read()
        crlf = "\r\n" in text
        o, n_ = (old.replace("\r\n", "\n").replace("\n", "\r\n"), new.replace("\r\n", "\n").replace("\n", "\r\n")) if crlf else (old, new)
        count = text.count(o)
        if count == 0:
            return 0, text
        if count > 1 and not all:
            return -count, text
        at = text.index(o)
        text = text.replace(o, n_) if all else text.replace(o, n_, 1)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        line = text.count("\n", 0, at) + 1
        lines = text.splitlines()
        span = n_.count("\n") + 1
        lo, hi = builtins_max(0, line - 3), min(len(lines), line + span + 2)
        return count if all else 1, "\n".join(f"{i + 1:>4}  {lines[i]}" for i in range(lo, hi))

    try:
        count, shown = await asyncio.to_thread(edit)
    except UnicodeDecodeError:
        raise Fail("UNSUPPORTED", f"{p} isn't UTF-8 text", "read_file(path) to see it")
    except PermissionError:
        raise Fail("BLOCKED", f"{p} is in use or read-only", "close the app using it")
    if count == 0:
        text = await asyncio.to_thread(_file_text_sync, p)
        first = old.strip().splitlines()[0].strip() if old.strip() else ""
        near = difflib.get_close_matches(first, [ln.strip() for ln in text.splitlines()], n=1, cutoff=0.5) if first else []
        raise Fail("NOT_FOUND", "old isn't in the file exactly (spaces and line breaks count)" + (f"; closest line: {near[0][:160]!r}" if near else ""),
                   f'read_file("{p}", find="{first[:40]}")')
    if count < 0:
        raise Fail("AMBIGUOUS", f"old is in the file {-count} times", "include a few surrounding lines in old, or all=true")
    ctx.written.add(str(p).lower())
    await asyncio.to_thread(_note_written, ctx, p)
    return ok(f"edited {p}: {count} replacement{'s' if count > 1 else ''}; around it now:\n{shown}")


def _stale_view(p: Path, rows: list, tags: list, bad: list, start: int, end: int) -> tuple[str, str]:
    """edit_lines' STALE result: which tag no longer matches and the file's current lines there with their tags, so the
    model can retry at once (all within call()'s 700 characters for a failure)."""
    total = len(rows)
    n0, h0 = bad[0]
    why = f"line {n0} is past its end ({total} lines)" if n0 > total else f"line {n0} isn't {n0}:{h0} any more"
    moved = ""
    if len({n for n, _ in tags}) >= 2:  # one 2-character tag can match a line by chance; two together don't
        for dd in sorted(range(-80, 81), key=abs):
            if dd and all(1 <= n + dd <= total and _hh(rows[n + dd - 1][0]) == h for n, h in tags):
                moved = f"; the tagged lines are now {dd:+d} away (lines were added or removed above)"
                start, end, n0 = start + dd, end + dd, start + dd
                break
    want = sorted({x for n in (start, end, n0) for x in (n - 1, n, n + 1) if 1 <= x <= total})[:9]
    lo, hi = (want[0], want[-1]) if want else (builtins_max(1, total - 5), total)
    try_ = f'edit_lines with these tags, or read_file("{p}", lines="{lo}-{hi}", anchors=true)'
    text = f"{p} changed since it was read: {why}{moved}. Now:\n"
    room = 640 - len(text) - len(try_)
    width = builtins_max(16, room // builtins_max(1, len(want)) - 12)
    view = "\n".join(f"{n}:{_hh(rows[n - 1][0])}|{_clip(rows[n - 1][0], width)}" for n in want)
    return text + view[:builtins_max(0, room)], try_


@action("edit_lines", group="FILE", summary="replace/insert/delete lines by number, checked by read_file anchors",
        params="""
        path s the file's path
        start i first line to change (insert: before it)
        end i? last line, inclusive (start-1 = insert)
        anchors s tags as read, like 12:k3-14:9a
        new_text s? the new lines ("" deletes them)
        """, cost=0.1, top="path,start,end,anchors,new_text", bang=True, fallback="edit_file(path, old, new)",
        risky=lambda a, c: "" if str(_path(str(a.get("path", "")))).lower() in c.written else f"edit the file {a.get('path')}",
        limits="use it after read_file(anchors=true); exact old text not needed; edit from the bottom up (lines below a change move)")
async def edit_lines(ctx: Ctx, path: str, start: int, anchors: str, end: int | None = None, new_text: str | None = None, **_) -> str:
    """Hashline edits: the model names whole lines by number and proves it saw them with their tags (read_file
    anchors=true), so it never copies old text: small models slip on its spaces, and every copied character costs a step's
    tokens. The same folder rules and asking as edit_file; the file's line breaks and encoding are kept."""
    if _ARTIFACT_LINK.match(str(path or "")):
        raise Fail("BLOCKED", "an artifact:// link is a saved tool result: read-only", "write_file(a path in the user's folders, text)")
    p = _path(path)
    writable(ctx, p)
    if _private(p):
        raise Fail("BLOCKED", "that is IO's own data (keys and history)", "ask_user")
    if not p.is_file():
        raise Fail("NOT_FOUND", f"{p} isn't a file", f'find_file("{p.name}")')
    if p.suffix.lower() in OFFICE + (".pdf",):
        raise Fail("UNSUPPORTED", f"{p.name} isn't a plain text file", f'open_path("{p}") and edit it in its app')
    if new_text is None:
        raise Fail("BAD_ARGS", 'new_text is missing (new_text="" deletes the lines)', "edit_lines(path, start, end, anchors, new_text)")
    start = int(start)
    tags = [(int(n), h.lower()) for n, h in re.findall(r"(\d+)\s*:\s*([0-9A-Za-z]{2})(?![0-9A-Za-z])", str(anchors or ""))]
    again = f'read_file("{p}", lines="{builtins_max(1, start - 2)}-{start + 8}", anchors=true)'
    if not tags and not (start == 1 and end == 0):  # (an empty file has no line to tag: insert at 1 needs none)
        raise Fail("BAD_ARGS", f"anchors={str(anchors)[:30]!r} has no N:hh tags (like 12:k3-14:9a)", again)
    if end is None:  # one line, or up to the last tagged line
        end = builtins_max([start] + [n for n, _ in tags])
    end = int(end)
    if start < 1 or end < start - 1:
        raise Fail("BAD_ARGS", f"start={start}, end={end}: end is the last line to change (start-1 to insert)", again)
    body = str(new_text).replace("\r\n", "\n").replace("\r", "\n")
    new = body.split("\n") if body else []
    if new and body.endswith("\n"):
        new.pop()  # a final line break ends the last new line, it doesn't add an empty one
    pasted = [x for x in new if x]
    stripped = bool(pasted) and all(_TAG_PREFIX.match(x) for x in pasted)
    if stripped:  # the tags read_file showed, copied into the new text
        new = [_TAG_PREFIX.sub("", x, count=1) for x in new]
    have = {n for n, _ in tags}
    key = str(p).lower()

    def apply() -> tuple:
        data = p.read_bytes()
        text, how = _decode(p, data)
        rows = _rows(text)
        total = len(rows)
        if start > total + 1 or end > total:
            raise Fail("BAD_ARGS", f"the file has {total} lines", f'read_file("{p}", lines="{builtins_max(1, total - 20)}-{total}", anchors=true)')
        if total:  # (an empty file has nothing to tag or check)
            if not tags:
                raise Fail("BAD_ARGS", "anchors has no N:hh tags (like 12:k3-14:9a)", again)
            if end >= start:
                missing = sorted({start, end} - have)
                if missing:
                    raise Fail("BAD_ARGS", f"anchors must tag line {' and '.join(map(str, missing))} as read", again)
            elif not ({start, start - 1} & have):
                raise Fail("BAD_ARGS", f"to insert before line {start}, anchors must tag line {start} or {start - 1}", again)
            bad = [(n, h) for n, h in tags if not (1 <= n <= total and _hh(rows[n - 1][0]) == h)]
            if bad:
                raise Fail("STALE", *_stale_view(p, rows, tags, bad, start, end))
        old = rows[start - 1:end] if end >= start else []
        if [t for t, _e in old] == new:
            return old, rows, None
        was = ctx.file_prints.get(key)
        fresh = not was or hashlib.sha1(data).hexdigest() == was[2]  # nothing else changed since the task read it
        crlf = text.count("\r\n")
        lf, cr = text.count("\n") - crlf, text.count("\r") - crlf
        nl = "\r\n" if crlf and crlf >= builtins_max(lf, cr) else "\r" if cr > lf else "\n"  # the file's own line break
        out = rows[:start - 1] + [(t, nl) for t in new] + rows[end:]
        for k in range(len(out) - 1):
            if not out[k][1]:
                out[k] = (out[k][0], nl)
        if out:  # the last line ends with a break only if the file's did
            out[-1] = (out[-1][0], (out[-1][1] or nl) if rows and rows[-1][1] else "")
        try:
            blob = _encode("".join(t + e for t, e in out), how)
        except UnicodeEncodeError:
            raise Fail("UNSUPPORTED", f"{p.name} is {how} text and new_text has characters it can't hold", "plain characters only") from None
        p.write_bytes(blob)
        return old, out, fresh

    try:
        old, out, fresh = await asyncio.to_thread(apply)
    except PermissionError:
        raise Fail("BLOCKED", f"{p} is in use or read-only", "close the app using it")
    if fresh is None:
        return ok(f"no change: lines {start}-{end} of {p} already read that")
    ctx.written.add(key)
    if fresh:  # else keep the old print: the file also changed elsewhere, and edit_file stays STALE until it's read again
        await asyncio.to_thread(_note_written, ctx, p)
    m, shift = len(new), len(new) - len(old)
    if end < start:
        what = f"inserted {m} line{'s' * (m != 1)} " + (f"before line {start}" if start <= len(out) - m else "at the end")
    elif m:
        what = f"replaced lines {start}-{end} ({len(old)}) with {m} line{'s' * (m != 1)}"
    else:
        what = f"deleted lines {start}-{end} ({len(old)})"
    view = []
    if start > 1:
        view.append(f"  {_tagged(out, start - 1, start - 1)[0]}"[:200])
    view += [f"- {start + i}  {_clip(t, 100)}" for i, (t, _e) in enumerate(old[:6])] + ([f"- …{len(old) - 6} more"] if len(old) > 6 else [])
    plus = [f"+ {x}"[:300] for x in _tagged(out, start, start + m - 1)]
    view += plus if m <= 40 else plus[:30] + [f"+ …{m - 35} more"] + plus[-5:]
    if start + m <= len(out):
        view.append(f"  {_tagged(out, start + m, start + m)[0]}"[:200])
    tail = (f"; lines after it moved {'down' if shift > 0 else 'up'} {abs(shift)}" if shift and start + m <= len(out) else "") + \
        ("; the N:hh| tags in new_text were left out" if stripped else "") + \
        ("; the file had also changed elsewhere since it was read: read_file it before edit_file" if not fresh else "")
    return ok(f"{what} in {p}{tail}. Now (new tags):\n" + "\n".join(view))


def _tree_size(p: Path) -> tuple[int, int]:
    total = files = 0
    t_end = time.time() + 20
    for dirpath, _d, filenames in os.walk(p, onerror=lambda e: None):
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
                files += 1
            except OSError:
                pass
        if time.time() > t_end:
            break
    return total, files


@action("file_op", group="FILE", summary="copy, move, rename, mkdir, recycle, zip, unzip, info or size",
        params="""
        op s copy|move|rename|mkdir|recycle|zip|unzip|info|size
        src s the file or folder
        dst s? destination path (or new name for rename)
        overwrite b? allow replacing an existing destination
        """, cost=0.5, star=True, top="op,src,dst?", bang=True, fallback="PowerShell(command)", timeout=60,
        risky=lambda a, c: _op_risky(a, c), limits="no permanent delete: recycle sends to the Recycle Bin")
async def file_op(ctx: Ctx, op: str, src: str, dst: str = "", overwrite: bool = False, **_) -> str:
    s = _path(src)
    if op != "mkdir" and not s.exists():
        raise Fail("NOT_FOUND", f"{s} doesn't exist", f'find_file("{s.name}")')
    if op == "info":
        st = s.stat()
        kind = "folder" if s.is_dir() else _size(s)
        return ok(f"{s}: {kind}, created {dt.datetime.fromtimestamp(st.st_ctime):%Y-%m-%d %H:%M}, modified "
                  f"{dt.datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M}" + (", read-only" if not os.access(s, os.W_OK) else ""))
    if op == "size":
        if s.is_file():
            return ok(f"{s} is {_size(s)} ({s.stat().st_size:,} bytes)")
        total, files = await asyncio.to_thread(_tree_size, s)
        return ok(f"{s} holds {files:,} files, {_human(total)} ({total:,} bytes)")
    if op == "mkdir":
        writable(ctx, s)
        s.mkdir(parents=True, exist_ok=True)
        return ok(f"folder {s} exists")
    writable(ctx, s)
    if op == "recycle":
        fn = "DeleteDirectory" if s.is_dir() else "DeleteFile"
        out, code = await ps(ctx, f"Add-Type -AssemblyName Microsoft.VisualBasic; [Microsoft.VisualBasic.FileIO.FileSystem]::{fn}("
                                  f"{_q(str(s))}, 'OnlyErrorDialogs', 'SendToRecycleBin'); 'ok'", timeout=30)
        if s.exists():
            raise Fail("BLOCKED", f"{s} is still there (in use?): {out[:120]}", "close the app using it")
        return ok(f"sent {s} to the Recycle Bin")
    if op == "zip":
        d = _path(dst) if dst else s.with_suffix(".zip") if s.is_file() else s.parent / (s.name + ".zip")
    elif op == "unzip":
        d = _path(dst) if dst else s.parent / s.stem
    elif op == "rename":
        if not dst:
            raise Fail("BAD_ARGS", "rename needs dst (the new name)", f'file_op("rename", "{src}", "new name.txt")')
        d = _path(dst) if re.search(r"[\\/]", dst) else s.with_name(dst)
    else:
        if not dst:
            raise Fail("BAD_ARGS", f"{op} needs dst", f'file_op("{op}", "{src}", "C:\\\\Users\\\\...")')
        d = _path(dst)
        if d.is_dir():
            d = d / s.name
    writable(ctx, d)
    if d.exists() and not overwrite and op != "unzip":
        raise Fail("BLOCKED", f"{d} already exists", f'file_op("{op}", "{src}", "{dst}", overwrite=true)')

    def run() -> None:
        d.parent.mkdir(parents=True, exist_ok=True)
        if op == "copy":
            if s.is_dir():
                shutil.copytree(s, d, dirs_exist_ok=overwrite)
            else:
                shutil.copy2(s, d)
        elif op in ("move", "rename"):
            if d.exists() and overwrite:
                os.replace(s, d)
            else:
                shutil.move(str(s), str(d))
        elif op == "zip":
            with zipfile.ZipFile(d, "w", zipfile.ZIP_DEFLATED) as z:
                if s.is_file():
                    z.write(s, s.name)
                else:
                    for f in s.rglob("*"):
                        z.write(f, f.relative_to(s.parent))
        elif op == "unzip":
            with zipfile.ZipFile(s) as z:
                root = d.resolve()
                for m in z.namelist():  # never write outside the target (zip slip)
                    if not (root / m).resolve().is_relative_to(root):
                        raise Fail("BLOCKED", f"{s.name} has an unsafe path inside: {m}")
                    if (root / m).exists() and not overwrite and not m.endswith("/"):
                        raise Fail("BLOCKED", f"{root / m} already exists", f'file_op("unzip", "{src}", "{dst}", overwrite=true)')
                z.extractall(root)

    try:
        await asyncio.to_thread(run)
    except PermissionError:
        raise Fail("BLOCKED", f"{s.name} is in use or read-only", "close the app using it")
    except zipfile.BadZipFile:
        raise Fail("UNSUPPORTED", f"{s.name} isn't a zip file")
    if not d.exists():
        raise Fail("NO_CHANGE", f"{op} ran but {d} isn't there", f'list_files("{d.parent}")')
    if op in ("move", "rename") and s.exists():
        return unsure(f"{d} exists but {s} is still there too", f'list_files("{s.parent}")')
    return ok(f"{ {'copy': 'copied', 'move': 'moved', 'rename': 'renamed', 'zip': 'zipped', 'unzip': 'unzipped'}[op] } {s.name} → {d} ({_size(d)})")


def _op_risky(args: dict, ctx: Ctx) -> str:
    op = args.get("op")
    if op in ("move", "rename", "recycle") or (args.get("overwrite") and op in ("copy", "unzip", "zip")):
        return f"{op} {args.get('src')}" + (f" to {args.get('dst')}" if args.get("dst") else "")
    return ""


LAUNCHABLE = re.compile(r"\.(exe|lnk|bat|cmd|com|msi|msix|appx|ps1|psm1|vbs|vbe|js|jse|wsf|wsh|hta|scr|pif|cpl|msc|reg|jar|py|pyw|url|appref-ms)$", re.I)


def _open_risky(args: dict, ctx: Ctx) -> str:
    """Opening a document or folder is harmless; running a program, script or installer (or opening something in an
    app given as a path) asks first."""
    path, app = str(args.get("path") or "").strip().strip("\"'"), str(args.get("app") or "").strip()
    if LAUNCHABLE.search(path):
        return f"run {path}"
    if app and (os.path.isabs(app) or "\\" in app or "/" in app or LAUNCHABLE.search(app)) and app.lower() not in APP_PATHS:
        return f"open {path} with the program {app}"
    return ""
APP_PATHS = {"notepad": "notepad.exe", "paint": "mspaint.exe", "word": "winword.exe", "excel": "excel.exe", "powerpoint": "powerpnt.exe",
             "wordpad": "write.exe", "vs code": "code", "code": "code", "explorer": "explorer.exe", "file explorer": "explorer.exe"}


def _app_exe_path(app: str) -> str:
    """An app's executable for opening a file in it: known names, PATH, then the App Paths registry."""
    import winreg
    name = APP_PATHS.get(app.lower().strip(), app.strip())
    if os.path.isabs(name) and os.path.exists(name):
        return name
    found = shutil.which(name) or shutil.which(name + ".exe")
    if found:
        return found
    exe = name if name.lower().endswith(".exe") else name + ".exe"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                return winreg.QueryValue(k, None)
        except OSError:
            continue
    return ""


def _shell_part(command: str) -> str:
    """The command line as the shell sees it, without quoted text or heredoc bodies: the risky-command words (del, rm,
    format...) are about shell commands, and Python code passed in quotes (".format(") was asked about as if it deleted."""
    s = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?.*?^\s*\1\s*$", " ", str(command or ""), flags=re.S | re.M)
    return re.sub(r"\"(?:[^\"\\]|\\.)*\"|'[^'\n]*'", " ", s)


@action("run_command", group="PC", summary="run a command line (python, git, npm, a test run) in a folder: its whole output and exit code",
        params="""
        command s the command line, e.g. python -m unittest -v
        folder s? where to run it
        timeout i? seconds it may take (120, at most 600)
        save_to s? also save the output to this file
        """, cost=1.0, star=True, top="command,folder?", fallback="PowerShell(command)", timeout=620,
        risky=lambda a, c: (f"run in {a.get('folder') or known_folder('home')}: {a.get('command', '')}"
                            if _h().risky_reason("PowerShell", {"command": _shell_part(a.get("command", ""))}, c.request) else ""),
        limits="cmd.exe syntax (2>&1, >, &&), not bash (no << heredocs); for a server or anything that keeps running use start_app")
async def run_command(ctx: Ctx, command: str, folder: str = "", timeout: int = 120, save_to: str = "", **_) -> str:
    """Programs the way a terminal runs them: stdout and stderr merged in order, the real exit code, no PowerShell
    wrapping (Windows PowerShell 5.1 turns each stderr line into an error record, so a run that redirected its test
    output to a file got "python : ... At line:2 char:40" noise in it and spent three test runs trying to get it clean)."""
    where = _path(folder) if folder else known_folder("home")
    if not where.is_dir():
        raise Fail("NOT_FOUND", f"{where} isn't a folder", f'file_op(op="mkdir", src="{where}")')
    limit = builtins_max(5, min(int(timeout or 120), 600))
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

    def go() -> tuple[int | None, str]:
        flags = 0x08000000  # CREATE_NO_WINDOW
        # one string, not a list: Python would escape the command's own quotes with backslashes, which cmd.exe doesn't
        # read (python -c "print(1)" lost its output); /s strips just the outer pair added here
        args = f'cmd.exe /d /s /c "{command}"'
        try:
            p = subprocess.Popen(args, cwd=str(where), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, creationflags=flags | 0x01000000)  # CREATE_BREAKAWAY_FROM_JOB
        except OSError:
            p = subprocess.Popen(args, cwd=str(where), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, creationflags=flags)
        try:
            out, _e = p.communicate(timeout=limit)
            code = p.returncode
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, creationflags=flags)
            out, _e = p.communicate()
            code = None
        try:
            text = out.decode("utf-8")
        except UnicodeDecodeError:
            text = out.decode("mbcs", errors="replace")  # an old tool writing in the ANSI code page
        return code, text.replace("\r\n", "\n").rstrip()

    code, text = await asyncio.to_thread(go)
    saved = ""
    if save_to:
        # the whole output, with the command's own exit code still reported: "tests > out.txt 2>&1 & type out.txt"
        # reported type's exit code (0) for a failing test run
        dest = _path(save_to) if (Path(save_to).is_absolute() or save_to.startswith(("%", "$", "~"))) else where / save_to
        writable(ctx, dest)
        await asyncio.to_thread(dest.write_text, text + "\n", encoding="utf-8")
        ctx.written.add(str(dest).lower())
        saved = f" (output saved to {dest})"
    budget = builtins_max(4000, ctx.page_chars or 4000)
    report, instead = ("", False)
    if code is not None and _DIAG_ANY.search(text):  # a compiler's errors and warnings: each once, and what they need
        report, instead = await asyncio.to_thread(_compile_report_sync, text, where, code != 0)
    if instead:
        link = _keep_whole(text)
        rest = "\n".join(ln for ln in text.splitlines() if not (_DIAG.match(ln) or _DIAG_NOFILE.match(ln)))  # listed above
        body = report + (f"\n\n[the whole output ({len(text):,} characters): {link}; its last lines other than the errors:]\n"
                         if link else "\n\n[the output's last lines other than the errors:]\n") + rest.strip()[-1200:]
    else:
        if len(text) > budget:  # the end of a run (the summary, the traceback) matters most
            text = text[:budget // 4] + f"\n[... {len(text) - budget} characters cut ...]\n" + text[-(budget * 3 // 4):]
        body = (text or "(no output)") + (f"\n\n{report}" if report else "")
    if code is None:
        raise Fail("TIMEOUT", f"still running after {limit}s, so it was stopped. Output so far:\n{body}",
                   "start_app for something that keeps running, or a larger timeout")
    if code != 0:
        raise Fail("FAILED", f"exit code {code} in {where}{saved}:\n{body}", "read the output above, fix the cause, run it again")
    return ok(f"exit code 0 in {where}{saved}:\n{body}")


@action("start_app", group="PC", summary="start something that keeps running after the task (a web server, a script); waits for its port",
        params="""
        command s what to run, e.g. python -m http.server 8123
        folder s? where to run it
        port i? wait until this port answers
        open b? also open http://localhost:port in the browser
        """, cost=2.0, star=True, top="command,folder?,port?", fallback='PowerShell("Start-Process ...")', timeout=60,
        risky=lambda a, c: _h().risky_reason("PowerShell", {"command": a.get("command", "")}, c.request),
        limits="its own window, so you can see and stop it; PowerShell's Start-Process ends with the task")
async def start_app(ctx: Ctx, command: str, folder: str = "", port: int = 0, open: bool = False, **_) -> str:
    """Started from IO's own process, outside the job Windows-MCP runs PowerShell in: when a task ends, the MCP client
    closes that job and everything started inside it (the servers of two graded runs died that way)."""
    where = _path(folder) if folder else known_folder("home")
    if not where.is_dir():
        raise Fail("NOT_FOUND", f"{where} isn't a folder", f'file_op(op="mkdir", src="{where}")')
    if port and _port_open(port):
        raise Fail("BLOCKED", f"port {port} is already in use", f'start_app on another port, or open http://localhost:{port}')
    q = lambda t: str(t).replace("'", "''")  # noqa: E731  PowerShell single-quoted text
    ps = f"Set-Location -LiteralPath '{q(where)}'; $host.UI.RawUI.WindowTitle = 'IO: {q(command[:60])}'; {command}"
    flags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(["powershell", "-NoProfile", "-NoExit", "-Command", ps], cwd=str(where),
                                creationflags=flags | 0x01000000)  # CREATE_BREAKAWAY_FROM_JOB, in case IO itself is in one
    except OSError:
        proc = subprocess.Popen(["powershell", "-NoProfile", "-NoExit", "-Command", ps], cwd=str(where), creationflags=flags)
    if not port:
        await asyncio.sleep(1.5)
        if proc.poll() is not None:
            raise Fail("UNSUPPORTED", f"it exited at once (code {proc.returncode})", "PowerShell(command) to see its error")
        return ok(f"started in its own window (pid {proc.pid}) in {where}; it keeps running after this task")
    for _ in range(40):
        if _port_open(port):
            break
        if proc.poll() is not None:
            raise Fail("UNSUPPORTED", f"it exited (code {proc.returncode}) before port {port} answered", "PowerShell(command) to see its error")
        await asyncio.sleep(0.5)
    else:
        return unsure(f"started (pid {proc.pid}) but port {port} didn't answer within 20 s", "check its window, or wait_until then open_path")
    url = f"http://localhost:{port}"
    if open:
        await asyncio.to_thread(os.startfile, url)
    return ok(f"running in its own window (pid {proc.pid}), {url} answers" + ("; opened it in the default browser" if open else "")
              + "; it keeps running after this task")


def _port_open(port: int) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", int(port))) == 0


# ======================================================================================================================
# the real API of installed libraries. A model writes library calls from memory, and its memory is of whatever version
# it was trained on: Dalamud moved LocalPlayer from IClientState to IObjectTable, and a model still writes the old one
# with full confidence. Nothing in its own knowledge says it's stale, so research never starts; the installed files
# are the truth, and a failed build now carries them.
# ======================================================================================================================

APILENS = HERE / "tools" / "apilens"
NO_WINDOW = 0x08000000
_apilens_lock = threading.Lock()
_refs_cache: dict[str, tuple[tuple, list[str], list[str]]] = {}


def _dotnet() -> str:
    exe = shutil.which("dotnet")
    if not exe:
        raise Fail("UNSUPPORTED", "the .NET SDK isn't installed (no dotnet on PATH)", 'web_answer("install the .NET SDK")')
    return exe


def _apilens_sync() -> Path:
    """tools/apilens/out/apilens.exe, built on first use (and again after its source changes)."""
    exe = APILENS / "out" / "apilens.exe"
    with _apilens_lock:
        newest = builtins_max((APILENS / n).stat().st_mtime for n in ("Program.cs", "apilens.csproj"))
        if exe.is_file() and exe.stat().st_mtime >= newest:
            return exe
        r = subprocess.run([_dotnet(), "build", str(APILENS), "-c", "Release", "-o", str(APILENS / "out"), "-nologo"],
                           capture_output=True, timeout=300, creationflags=NO_WINDOW)
        if r.returncode != 0 or not exe.is_file():
            raise Fail("FAILED", "IO's API reader (tools/apilens) didn't build:\n" + r.stdout.decode("utf-8", "replace")[-1500:],
                       f'run_command("dotnet build -c Release -o out", folder="{APILENS}")')
        exe.touch()  # an up-to-date build copies nothing, and the check above compares times
        return exe


def _lens_sync(search: list[str], refs: list[str], mode: str, query: str = "", member: str = "", budget: int = 12000) -> str:
    """apilens on these dlls: mode type (one type in full), find (names containing a word) or overview."""
    exe = _apilens_sync()
    req = Path(os.environ.get("TEMP") or HERE / "data") / f"io-apilens-{os.urandom(4).hex()}.json"
    req.write_text(json.dumps({"search": search, "refs": refs, "mode": mode, "query": query, "member": member, "max": budget}),
                   encoding="utf-8")
    try:
        r = subprocess.run([str(exe), str(req)], capture_output=True, timeout=120, creationflags=NO_WINDOW)
    finally:
        req.unlink(missing_ok=True)
    text = r.stdout.decode("utf-8", "replace").replace("\r\n", "\n").strip()
    if r.returncode != 0 and not text:
        raise Fail("FAILED", "IO's API reader failed: " + r.stderr.decode("utf-8", "replace")[-800:], "find_file the library's .dll and pass it as of=")
    return text


def _project_refs_sync(proj: Path) -> tuple[list[str], list[str]]:
    """(the dlls a project compiles against other than .NET itself, every dll it compiles against): exactly what the
    compiler sees, from MSBuild's own reference resolution (packages, an SDK's own references like Dalamud's, project
    references). The project's own libraries come first."""
    assets = proj.parent / "obj" / "project.assets.json"
    stamp = lambda: (proj.stat().st_mtime, assets.stat().st_mtime if assets.exists() else 0)  # noqa: E731
    key = str(proj).lower()
    hit = _refs_cache.get(key)
    if hit and hit[0] == stamp():
        return hit[1], hit[2]
    base = [_dotnet(), "msbuild", str(proj), "-nologo", "-restore", "-t:ResolveAssemblyReferences", "-getItem:ReferencePath"]

    def run(extra: list[str]) -> tuple[int, str]:
        r = subprocess.run(base + extra, capture_output=True, timeout=300, creationflags=NO_WINDOW, cwd=str(proj.parent))
        return r.returncode, r.stdout.decode("utf-8", "replace")

    code, out = run([])
    source = proj.read_text(encoding="utf-8", errors="replace")
    if code != 0 and "TargetFramework" in out:  # a multi-target project: ask about its first framework
        m = re.search(r"<TargetFrameworks>\s*([^;<\s]+)", source)
        if m:
            code, out = run([f"-p:TargetFramework={m.group(1)}"])
    try:
        items = json.loads(out[out.index("{"):])["Items"]["ReferencePath"]
    except (ValueError, KeyError, TypeError):
        raise Fail("FAILED", f"MSBuild couldn't list {proj.name}'s references:\n{_clip(out.strip(), 1500)}",
                   f'run_command("dotnet build", folder="{proj.parent}")') from None
    refs = [i["Identity"] for i in items if i.get("Identity")]
    named = source.lower()
    search = sorted((i["Identity"] for i in items if i.get("Identity") and not i.get("FrameworkReferenceName")),
                    key=lambda p: 0 if Path(p).stem.lower() in named else 1)
    _refs_cache[key] = (stamp(), search, refs)
    return search, refs


def _nuget_dlls(name: str) -> list[str]:
    """A package's dlls from the local NuGet cache: its newest stable version, the newest framework it ships for."""
    root = Path(os.environ.get("NUGET_PACKAGES") or Path.home() / ".nuget" / "packages") / name.strip().lower()
    if not root.is_dir():
        return []

    def version(d: Path) -> tuple:
        return ("-" not in d.name, [int(x) for x in re.findall(r"\d+", d.name)])

    def framework(d: Path) -> tuple:
        n = d.name.lower()
        m = re.match(r"net(\d+)\.(\d+)", n)
        return (2, int(m.group(1)), int(m.group(2))) if m else (1, 0, 0) if n.startswith("netstandard") else (0, 0, 0)

    for v in sorted((d for d in root.iterdir() if d.is_dir()), key=version, reverse=True):
        libs = [d for d in (v / "lib").glob("*") if d.is_dir() and any(d.glob("*.dll"))] if (v / "lib").is_dir() else []
        if libs:
            return [str(p) for p in builtins_max(libs, key=framework).glob("*.dll")]
    return []


# a Python module's API, printed by the Python that has it installed
_PY_LENS = r'''
import importlib, inspect, sys
mod, typ, find, budget = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
out = []
def doc(o, w=160):
    d = (inspect.getdoc(o) or "").strip()
    return ("  # " + d.splitlines()[0][:w]) if d else ""
def sig(o):
    try:
        return str(inspect.signature(o))
    except (TypeError, ValueError):
        return "(...)"
def line(o, name):
    if isinstance(o, (staticmethod, classmethod)):
        o = o.__func__
    if isinstance(o, property):
        return name + "  (property)"
    if inspect.isclass(o):
        return "class " + name + sig(o)
    if callable(o):
        return name + sig(o)
    return name + " = " + repr(o)[:80]
def public(o):
    names = getattr(o, "__all__", None) if inspect.ismodule(o) else None
    for k in (names or dir(o)):
        if k.startswith("_") and k != "__init__":
            continue
        try:
            yield k, (inspect.getattr_static(o, k) if inspect.isclass(o) else getattr(o, k))
        except Exception:
            continue
m = importlib.import_module(mod)
ver = getattr(m, "__version__", "")
if not ver:
    try:
        from importlib.metadata import version
        ver = version(mod.split(".")[0])
    except Exception:
        pass
out.append(f"{mod} {ver} ({getattr(m, '__file__', '')})")
if typ:
    o = m
    for part in typ.split("."):
        try:
            o = getattr(o, part)
        except AttributeError:
            try:
                o = importlib.import_module(o.__name__ + "." + part)
            except Exception:
                out.append(f"no {part} in {getattr(o, '__name__', mod)}")
                o = None
                break
    if o is not None:
        out.append(line(o, typ) + doc(o, 400))
        if inspect.isclass(o) or inspect.ismodule(o):
            for k, v in public(o):
                out.append("    " + line(v, k) + doc(getattr(v, "__func__", v)))
elif find:
    w = find.lower()
    for name, mm in list(sys.modules.items()):
        if not (name == mod or name.startswith(mod + ".")):
            continue
        for k, v in list(vars(mm).items()):
            if k.startswith("_"):
                continue
            if w in k.lower():
                out.append(f"  {name}.{line(v, k)}")
            if inspect.isclass(v) and getattr(v, "__module__", "") == name:
                for k2, v2 in vars(v).items():
                    if not k2.startswith("_") and w in k2.lower():
                        out.append(f"  {name}.{k}.{line(v2, k2)}")
    if len(out) == 1:
        out.append("nothing by that name in the module or its loaded submodules")
else:
    for k, v in public(m):
        out.append("  " + line(v, k) + doc(v, 100))
print("\n".join(out)[:budget])
'''


def _py_lens_sync(module: str, python: str, type_: str, find: str, budget: int) -> str:
    exe = python.strip().strip('"') if python else ""
    if not exe:
        found = shutil.which("python") or shutil.which("py") or ""
        exe = sys.executable if not found or "WindowsApps" in found else found  # the Store's stub only opens the Store
    import workshop  # noqa: PLC0415
    deps = os.pathsep.join(workshop.deps_paths())  # packages installed for workshop tools can be looked up too
    try:
        r = subprocess.run([exe, "-c", _PY_LENS, module, type_, find, str(budget)], capture_output=True, timeout=60,
                           creationflags=NO_WINDOW, cwd=str(Path.home()),
                           env={**os.environ, "PYTHONIOENCODING": "utf-8", **({"PYTHONPATH": deps} if deps else {})})
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Fail("FAILED", f"couldn't run {exe}: {e}", "python= the full path of the project's python.exe") from None
    text = r.stdout.decode("utf-8", "replace").replace("\r\n", "\n").strip()
    if r.returncode != 0:
        why = r.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no output"]
        raise Fail("NOT_FOUND", f"{module}: {why[0]} (asked {exe})",
                   "of= a .csproj, .dll or folder for .NET; for Python, python= the project's own python.exe")
    return text


@action("api_lookup", group="PC", summary="a library's real API as installed (.NET project/dll, NuGet, Python)",
        params="""
        of s .csproj, .dll, folder, NuGet id, module
        type s? one type in full; Type.Member for one
        find s? a word to search type/member names for
        python s? the Python that has the module
        """, cost=1.0, star=True, top="of,type?,find?", timeout=420, hide=("python",),
        desc="Read from the installed files, so it matches the version the code builds against. of= a .csproj gives exactly "
             "what it compiles against (an SDK's own libraries included). type= lists members with signatures, doc summaries "
             "and [Obsolete] notes naming replacements; find= searches names; neither: the namespaces.",
        limits="the installed files, not the web; with neither type nor find: an overview of its namespaces")
async def api_lookup(ctx: Ctx, of: str, type: str = "", find: str = "", python: str = "", **_) -> str:
    """Ground truth for code against a library: what the version on this PC really has, read from its files. A .csproj
    gives exactly the libraries it compiles against (an SDK's own, like Dalamud's, included)."""
    budget = builtins_max(4000, min(ctx.page_chars or 12000, 16000))
    target = str(of or "").strip().strip('"')
    if not target:
        raise Fail("BAD_ARGS", "of= is required: a project, dll, folder, NuGet package or Python module",
                   'api_lookup(of="C:\\\\path\\\\App.csproj", find="name")')
    mode, query = ("type", type) if type else ("find", find) if find else ("overview", "")
    if not re.search(r"[\\/:]|\.(dll|csproj|fsproj|vbproj)$", target, re.I):
        dlls = await asyncio.to_thread(_nuget_dlls, target)
        cached = (Path(os.environ.get("NUGET_PACKAGES") or Path.home() / ".nuget" / "packages") / target.lower()).is_dir()
        if not dlls and cached:
            raise Fail("NOT_FOUND", f"the NuGet package {target} has no libraries of its own (an MSBuild SDK or a build tool)",
                       "api_lookup(of=a project that uses it): that lists what the project compiles against")
        if not dlls:  # not a cached NuGet package: a Python module
            text = await asyncio.to_thread(_py_lens_sync, target, python, type, find, budget)
            return ok(f"Python {target}, as installed:\n{text}")
        text = await asyncio.to_thread(_lens_sync, dlls, [], mode, query, "", budget)
        return ok(f"NuGet {target} ({Path(dlls[0]).parent}):\n{text}")
    p = _path(target)
    if not p.exists():
        raise Fail("NOT_FOUND", f"{p} doesn't exist", f'find_file("{p.name}")')
    if p.is_dir():
        projects = [q for ext in ("*.csproj", "*.fsproj", "*.vbproj") for q in p.glob(ext)]
        if len(projects) == 1:
            p = projects[0]
        else:
            dlls = [str(q) for q in p.glob("*.dll")]
            if not dlls:
                raise Fail("NOT_FOUND", f"no .dll or single project file in {p}", f'list_files("{p}")')
            text = await asyncio.to_thread(_lens_sync, dlls, [], mode, query, "", budget)
            return ok(text)
    if p.suffix.lower() in (".csproj", ".fsproj", ".vbproj"):
        search, refs = await asyncio.to_thread(_project_refs_sync, p)
        if not search:
            raise Fail("NOT_FOUND", f"{p.name} compiles against nothing but .NET itself", "api_lookup on a .dll, or the package's name")
        text = await asyncio.to_thread(_lens_sync, search, refs, mode, query, "", budget)
        return ok(f"what {p.name} compiles against:\n{text}")
    if p.suffix.lower() != ".dll":
        raise Fail("BAD_ARGS", f"{p.name} isn't a .dll or a project file", "of= a .csproj, a .dll, a folder, a NuGet id or a Python module")
    text = await asyncio.to_thread(_lens_sync, [str(p)], [], mode, query, "", budget)
    if mode != "overview" and "Nothing by that name" in text[:800]:
        # a library is often several dlls side by side (Lumina's RowRef is in Lumina.dll, not Lumina.Excel.dll): look there
        siblings = [str(q) for q in p.parent.glob("*.dll") if q != p]
        if siblings:
            more = await asyncio.to_thread(_lens_sync, [str(p)] + siblings, [], mode, query, "", budget)
            if "Nothing by that name" not in more[:800]:
                return ok(f"not in {p.name}; in the dlls beside it:\n{more}")
    return ok(text)


# ======================================================================================================================
# IO about itself, and its workshop: what it can do (from its own code), and tools it builds for itself with the
# user's OK when it hits a wall (workshop.py has the whole flow)
# ======================================================================================================================

SOURCE_MAP = [
    ("boss.py", "the agent loop: brains (local Glimmer, NVIDIA race), effort levels, planning, checks, Ultracode helpers, compaction"),
    ("actions.py", "every action: what it does, its checks and limits, routing of a request to a menu of tools"),
    ("nim.py", "NVIDIA's models: pacing, health, reasoning switches per model, tests"),
    ("app.py", "the panel's server: tasks, chats, goals, schedules, triggers, approvals, phone access"),
    ("panel.html", "the window you and the user see"),
    ("plugins.py", "MCP plugins and skills, and starting workshop tools"),
    ("workshop.py", "the workshop: proposing, building, testing and turning on tools IO builds for itself"),
    ("workshop_host.py", "runs one workshop tool as an MCP server"),
    ("learned.py", "playbooks IO writes for itself after runs"),
]


@action("about_io", group="END", summary="what IO itself can do: its tools and limits, effort, plugins, its own code",
        params="""
        topic s? a word: only the tools about it
        """, cost=0.1, star=True, top="topic?",
        limits="IO's program files are read-only to IO: read them, never change them; a missing ability: propose_tool")
async def about_io(ctx: Ctx, topic: str = "", **_) -> str:
    """IO's own manual, generated from its code, so it is never out of date with what IO really has."""
    import workshop  # noqa: PLC0415 - workshop imports nothing of IO's
    words = [w.lower() for w in re.findall(r"\w{3,}", topic or "")]
    lines: list[str] = []
    level = ctx.options.get("effort", "")
    if not words:
        lines.append(f"IO: a Windows desktop agent. This task runs at effort {level or '?'} (Low: local only; Medium: hard "
                     "parts to NVIDIA's models; High: NVIDIA brain, more steps, a check; Max: High plus Ultracode helpers).")
        lines.append(f"Today is {time.strftime('%Y-%m-%d')}. The models' knowledge comes from their training, which may be a year or "
                     "more older: check versions on this PC (api_lookup) or the web.\n")
    for g in GROUPS:
        acts = [a for a in REGISTRY.values() if a.group == g and "internal" not in a.modes]
        if words:
            acts = [a for a in acts if any(w in f"{a.name} {a.summary} {a.limits} {a.desc}".lower() for w in words)]
        if not acts:
            continue
        lines.append(GROUP_HEAD.get(g, g))
        for a in sorted(acts, key=lambda a: a.name):
            lines.append(f"  {a.signature()}: {a.summary}" + (f" [{a.limits}]" if a.limits and words else ""))
    if not words:
        try:
            on = [p for p, s in json.loads((HERE / "data" / "plugins.json").read_text(encoding="utf-8")).get("installed", {}).items()
                  if s.get("enabled", True)]
        except (OSError, ValueError):
            on = []
        lines.append("\nPlugins on: " + (", ".join(on) if on else "none"))
        built = workshop.public()
        lines.append("Tools IO built itself (workshop): " + ("; ".join(f"{w['name']} ({w['status']}): {w['does']}" for w in built)
                                                             if built else "none yet"))
        lines.append("\nIO's own code (read it with read_file to see how something works; it is read-only to IO):")
        lines += [f"  {HERE / name}: {what}" for name, what in SOURCE_MAP]
        lines.append("\nWhen a task needs an ability no tool here gives (not just information or the user's decision), call "
                     "propose_tool: the user is asked whether IO may build that tool in its workshop.")
    budget = builtins_max(4000, min(ctx.page_chars or 12000, 16000))
    text = "\n".join(lines) if lines else f"no tool mentions {topic!r}; about_io() lists everything"
    return ok(text if len(text) <= budget else text[:budget] + "\n[... more: about_io(topic=...) narrows it]")


@action("propose_tool", group="END", summary="ask the user to let IO build a tool it lacks (in its workshop)",
        params="""
        name s a short name for the tool
        does s what it would do, one sentence
        why s what in this task needed it
        """, cost=0.1, star=True, top="name,does,why",
        limits="only for a missing ability, not missing information; the user approves building it, then turning it on")
async def propose_tool(ctx: Ctx, name: str, does: str, why: str = "", **_) -> str:
    cb = ctx.options.get("workshop_propose")
    if cb is None:
        raise Fail("UNSUPPORTED", "the workshop isn't available in this run", "say in done what tool would have helped")
    return ok(await cb(str(name), str(does), str(why)))


@action("workshop_test", group="PC", summary="check a workshop tool against the rules and run its test_tool.py",
        params="""
        tool s the tool's id (its folder name)
        """, cost=5.0, top="tool", timeout=200, limits="only tools in IO's workshop folder; the test runs up to 120 s")
async def workshop_test(ctx: Ctx, tool: str, **_) -> str:
    import workshop  # noqa: PLC0415
    try:
        r = await asyncio.to_thread(workshop.test_sync, str(tool).strip())
    except ValueError as e:
        raise Fail("BAD_ARGS", str(e), "the id from the build task (the folder's name)") from None
    names = ", ".join(f"{workshop.prefix(tool)}_{t['name']}{t['signature']}" for t in r.get("tools") or [])
    if not r["ok"]:
        raise Fail("FAILED", r["output"], "fix tool.py or test_tool.py, then workshop_test again")
    return ok(f"passed. Its tools: {names}\n{r['output'][-1500:]}\nNext: tool_ready(tool=\"{tool}\", summary=...)")


@action("tool_ready", group="PC", summary="hand a tested workshop tool to the user to turn on",
        params="""
        tool s the tool's id
        summary s what it does and how you tested it
        """, cost=0.1, top="tool,summary", limits="only after workshop_test passed on the files as they are now")
async def tool_ready(ctx: Ctx, tool: str, summary: str, **_) -> str:
    import workshop  # noqa: PLC0415
    tid = str(tool).strip()
    try:
        entry, now = workshop.get(tid), await asyncio.to_thread(workshop.files_hash, tid)
    except ValueError as e:
        raise Fail("BAD_ARGS", str(e), "the id from the build task") from None
    if entry is None:
        raise Fail("NOT_FOUND", f"no workshop tool {tid}", "about_io() lists them")
    test = entry.get("test") or {}
    if not test.get("ok") or test.get("hash") != now:
        raise Fail("NEEDS", "its last workshop_test didn't pass on the files as they are now", f'workshop_test(tool="{tid}")')
    cb = ctx.options.get("workshop_ready")
    if cb is None:
        raise Fail("UNSUPPORTED", "the workshop isn't available in this run", "say in done that the tool is built and tested")
    return ok(await cb(tid, str(summary), now))


# --- installs and updates (pip, npm, winget), each with the user's OK on the PC, like a coding agent asks. Never into
# IO's own Python: an install there could break IO itself (a newer pydantic under the mcp it runs on); a workshop tool's
# packages go in the tool's own folder instead

INSTALL_QUESTION = "IO wants to install"  # app.py: answering this one is for the PC, not a paired phone


def _get_json_sync(url: str) -> dict | None:
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "IO"}), timeout=15) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _package_facts_sync(manager: str, name: str, version: str) -> tuple[str, str]:
    """(the exact version that will be installed, one line about the package from its registry). Fails for a name the
    registry doesn't know, so a misspelt (or look-alike) name stops here instead of installing something else."""
    if manager == "pip":
        info = _get_json_sync(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
        if info is None:
            raise Fail("NOT_FOUND", f"PyPI has no package named {name}", "web_search the package's exact name")
        if version and version not in (info.get("releases") or {}):
            raise Fail("NOT_FOUND", f"{name} has no version {version} on PyPI (newest: {info['info']['version']})", "leave version out")
        i = info["info"]
        home = (i.get("project_urls") or {}).get("Homepage") or i.get("home_page") or f"https://pypi.org/project/{name}/"
        return version or i["version"], f"\"{_clip(i.get('summary') or 'no description', 160)}\" ({home})"
    if manager == "npm":
        info = _get_json_sync(f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@')}")
        if info is None:
            raise Fail("NOT_FOUND", f"npm has no package named {name}", "web_search the package's exact name")
        latest = (info.get("dist-tags") or {}).get("latest", "")
        if version and version not in (info.get("versions") or {}):
            raise Fail("NOT_FOUND", f"{name} has no version {version} on npm (newest: {latest})", "leave version out")
        return version or latest, f"\"{_clip(info.get('description') or 'no description', 160)}\" (https://www.npmjs.com/package/{name})"
    r = subprocess.run(["winget", "show", "--id", name, "-e", "--disable-interactivity"] + (["--version", version] if version else []),
                       capture_output=True, timeout=60, creationflags=NO_WINDOW)
    out = r.stdout.decode("utf-8", "replace")
    m = re.search(r"Found (.+?) \[", out)
    if r.returncode != 0 or not m:
        raise Fail("NOT_FOUND", f"winget has no package with the id {name}: {_clip(out.strip(), 300)}", 'run_command("winget search <words>")')
    ver = re.search(r"(?m)^Version:\s*(.+)$", out)
    pub = re.search(r"(?m)^Publisher:\s*(.+)$", out)
    return (version or (ver.group(1).strip() if ver else "newest")), f"{m.group(1)}" + (f" by {pub.group(1).strip()}" if pub else "")


@action("install_package", group="PC", summary="install or update a package or program, after the user's OK (pip, npm, winget)",
        params="""
        manager s pip|npm|winget
        name s the package's exact name or id
        why s what it's needed for
        version s? a version (default: the newest)
        for_tool s? pip: a workshop tool's id (installs in its folder)
        folder s? pip: a project with a .venv; npm: the project
        update b? update it if it's already installed
        """, cost=30.0, star=True, top="manager,name,why", timeout=900,
        limits="asks the user first, every time, on the PC; never into IO's own Python; npm only into a project folder")
async def install_package(ctx: Ctx, manager: str, name: str, why: str, version: str = "", for_tool: str = "", folder: str = "",
                          update: bool = False, **_) -> str:
    import workshop  # noqa: PLC0415
    manager, name, version = str(manager).strip().lower(), str(name).strip(), str(version or "").strip()
    if manager not in ("pip", "npm", "winget"):
        raise Fail("BAD_ARGS", "manager must be pip, npm or winget", 'install_package(manager="pip", name=..., why=...)')
    if not re.fullmatch(r"@?[\w.\-]+(/[\w.\-]+)?", name):
        raise Fail("BAD_ARGS", f"{name!r} isn't a package name", "the exact name from its registry")
    exact, about = await asyncio.to_thread(_package_facts_sync, manager, name, version)
    own = str(_real(HERE)).lower()
    cwd = None
    if manager == "pip":
        uv = shutil.which("uv")
        if for_tool:
            try:
                d = workshop.folder(str(for_tool).strip())
            except ValueError as e:
                raise Fail("BAD_ARGS", str(e), "the tool's id") from None
            if not d.is_dir():
                raise Fail("NOT_FOUND", f"no workshop tool {for_tool}", "about_io() lists them")
            where = f"the workshop tool {for_tool}'s own folder (not IO's own Python)"
            target = ["--target", str(d / "_deps"), "--python", workshop.python()]
        else:
            py = _path(folder) / ".venv" / "Scripts" / "python.exe" if folder else None
            if folder and not py.is_file():
                raise Fail("NOT_FOUND", f"{folder} has no .venv", f'run_command("uv venv", folder="{folder}") first')
            exe = str(py) if py else (shutil.which("python") or "")
            if not exe or "WindowsApps" in exe:
                raise Fail("NOT_FOUND", "no Python on PATH to install into", "folder= a project with a .venv")
            if str(_real(Path(exe))).lower().startswith(own + "\\"):
                raise Fail("BLOCKED", "that is IO's own Python: an install there could break IO", "for_tool= a workshop tool, or a project's folder=")
            where = f"the Python at {exe}"
            target = ["--python", exe] + ([] if py else ["--system"])
        spec = f"{name}=={exact}"  # exactly the version the user was shown
        cmd = ([uv, "pip", "install"] if uv else [workshop.python(), "-m", "pip", "install"]) + (["--upgrade"] if update else []) + target + [spec]
    elif manager == "npm":
        if not folder or not _path(folder).is_dir():
            raise Fail("BAD_ARGS", "npm installs go into a project: folder= its folder", "IO doesn't install npm packages globally")
        cwd = str(_path(folder))
        if cwd.lower().startswith(own + "\\") or cwd.lower() == own:
            raise Fail("BLOCKED", "that is IO's own program folder", "a project of the user's")
        where = f"the project {cwd}"
        cmd = ["npm.cmd", "install", f"{name}@{exact if version else 'latest'}"]
    else:
        where = "this PC (a program; Windows may ask for admin rights)"
        cmd = (["winget", "upgrade" if update else "install", "--id", name, "-e", "--silent", "--disable-interactivity",
                "--accept-package-agreements", "--accept-source-agreements"] + (["--version", version] if version else []))
    question = (f"{INSTALL_QUESTION} {name} {exact} with {manager}{' (an update)' if update else ''}: {about}. "
                f"Into: {where}. Why: {_clip(str(why), 300)}. "
                + ("Installing it accepts the package's license terms. " if manager == "winget" else "") + "Allow it? (yes/no)")
    if ctx.ask is None:
        later = ctx.options.get("approve_later")
        if callable(later):  # nobody is watching: it waits in Approvals with everything above
            return ok(await later("install_package", {"manager": manager, "name": name, "version": version, "why": why,
                                                       "for_tool": for_tool, "folder": folder, "update": update},
                                  question.removeprefix(f"{INSTALL_QUESTION} ").removesuffix(" Allow it? (yes/no)")))
        raise Fail("NEEDS", "an install needs the user's OK and nobody can answer here", "say in done what to install and why")
    answer = await ctx.ask(question)
    if not str(answer).strip().lower().startswith("y"):
        return unsure(f"the user didn't allow installing {name}", "do without it, or say in done what it would have helped with")

    def run() -> tuple[int, str]:
        r = subprocess.run(cmd, capture_output=True, timeout=840, cwd=cwd, creationflags=NO_WINDOW)
        return r.returncode, (r.stdout + b"\n" + r.stderr).decode("utf-8", "replace").replace("\r\n", "\n").strip()

    code, out = await asyncio.to_thread(run)
    if code != 0:
        raise Fail("FAILED", f"the install failed (exit code {code}):\n{out[-2500:]}", "read why above; a different version or package, or ask_user")
    if manager == "pip" and for_tool:
        d = workshop.folder(str(for_tool).strip())
        req = d / "requirements.txt"
        lines = [ln for ln in (req.read_text(encoding="utf-8").splitlines() if req.is_file() else [])
                 if ln.split("==")[0].strip().lower() != name.lower()]
        req.write_text("\n".join(lines + [f"{name}=={exact}"]) + "\n", encoding="utf-8")
        return ok(f"installed {name} {exact} into {d / '_deps'} (recorded in requirements.txt); `import` it in tool.py, "
                  f'then workshop_test(tool="{for_tool}")\n{out[-800:]}')
    return ok(f"installed {name} {exact} into {where}\n{out[-1500:]}")


# --- compiler errors in a build's output (dotnet/MSBuild, tsc): each error once, with its source line, and for an error
# that says a name doesn't exist, the library's real API right in the result

_DIAG = re.compile(r"^\s*(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)(?:,\d+,\d+)?\)\s*:\s*(?P<sev>error|warning)\s+(?P<code>[A-Z]{2,}\d+)\s*:\s*"
                   r"(?P<msg>.*?)(?:\s+\[(?P<proj>[^\[\]]+?\.\w*proj)(?:::[^\]]*)?\])?\s*$")
_DIAG_NOFILE = re.compile(r"^\s*(?:.*?:\s+)?(?P<sev>error)\s+(?P<code>(?:NU|MSB|NETSDK|CS)\d+)\s*:\s*(?P<msg>.*?)"
                          r"(?:\s+\[(?P<proj>[^\[\]]+?\.\w*proj)(?:::[^\]]*)?\])?\s*$")
_DIAG_ANY = re.compile(r"\b(?:error|warning) [A-Z]{2,}\d+\s*:")
# "that name isn't in the library as installed": the result then shows the library's real API
_API_ERRORS = {"CS0117", "CS1061", "CS0246", "CS0234", "CS0426", "CS1501", "CS7036", "CS1739", "CS0103", "CS0122"}
_ERROR_HINTS = {
    "CS1705": "A library here is built for a newer .NET than the project targets: raise the project's TargetFramework (or the "
              "version of the SDK that sets it, as in Sdk=\"Name/version\") to match the installed library.",
    "NETSDK1045": "This .NET SDK can't target that framework: target one it can, or install the newer SDK.",
    "NU1101": "No package by that id: check it with run_command(\"dotnet package search <id> --exact-match\").",
    "NU1102": "That version of the package doesn't exist: run_command(\"dotnet package search <id> --exact-match\") lists the real ones.",
    "MSB3027": "The output file is locked by a running program (a game or app that loaded it): unload or reload it there, then build again.",
    "MSB3021": "The output file is locked by a running program (a game or app that loaded it): unload or reload it there, then build again.",
}


def _api_queries(errors: list[dict]) -> list[tuple[str, str, str]]:
    """What to look up for errors about names: [(mode, query, member)], at most 4."""
    out: list = []

    def add(q: tuple) -> None:
        if q[1] and q not in out and len(out) < 4:
            out.append(q)

    def short(s: str) -> str:  # 'Dalamud.Plugin.Services.IClientState?' -> IClientState; 'List<int>' -> List
        return re.sub(r"<.*", "", s.strip().rstrip("?")).split(".")[-1]

    for d in errors:
        code, msg = d["code"], d["msg"]
        if code in ("CS1061", "CS0117") and (m := re.search(r"'([^']+)' does not contain a definition for '([^']+)'", msg)):
            add(("type", short(m.group(1)), ""))
            add(("find", m.group(2), ""))
        elif code == "CS0246" and (m := re.search(r"name '([^'<]+)", msg)):
            add(("find", m.group(1), ""))
        elif code in ("CS0234", "CS0426") and (m := re.search(r"name '([^'<]+)'", msg)):
            add(("find", m.group(1), ""))
        elif code == "CS1501" and (m := re.search(r"method '([^'<]+)'", msg)):
            add(("find", m.group(1), ""))
        elif code == "CS1739" and (m := re.search(r"overload for '([^'<]+)'", msg)):
            add(("find", m.group(1), ""))
        elif code == "CS7036" and (m := re.search(r"of '([\w.]+?)\.(\w+)(?:<[^>]*>)?\(", msg)):
            add(("type", short(m.group(1)), m.group(2)))
        elif code == "CS0103" and (m := re.search(r"name '([A-Z]\w*)' does not exist", msg)):
            add(("find", m.group(1), ""))
        elif code == "CS0122" and (m := re.search(r"'([^'(]+)", msg)):
            add(("find", m.group(1).split(".")[-1], ""))
    return out


def _compile_report_sync(text: str, where: Path, failed: bool) -> tuple[str, bool]:
    """(a report on a build's errors and warnings, whether it replaces the raw output). Errors are listed once each
    (MSBuild repeats them in its summary) with the line of code they point at."""
    errors: list[dict] = []
    warns: list[dict] = []
    seen = set()
    for raw in text.splitlines():
        m = _DIAG.match(raw) or _DIAG_NOFILE.match(raw)
        if not m:
            continue
        d = {"file": "", "line": "", "col": "", "proj": None, **{k: v for k, v in m.groupdict().items() if v is not None}}
        key = (d["file"], d["line"], d["col"], d["code"], d["msg"][:120])
        if key not in seen:
            seen.add(key)
            (errors if d["sev"] == "error" else warns).append(d)
    obsolete = [w for w in warns if w["code"] in ("CS0618", "CS0612")]
    if not failed or not errors:
        if not obsolete:
            return "", False
        lines = [f"  {Path(w['file']).name}:{w['line']} {_clip(w['msg'], 220)}" for w in obsolete[:10]]
        return ("Built, but it uses parts of the library marked obsolete (they go away in a later version; the message "
                "usually names the replacement):\n" + "\n".join(lines)), False

    def rel(f: str) -> str:
        try:
            return str(Path(f).resolve().relative_to(where.resolve()))
        except (ValueError, OSError):
            return Path(f).name if f else ""

    sources: dict[str, list[str]] = {}

    def source_line(d: dict) -> str:
        f = d["file"]
        if not d["line"] or not f:
            return ""
        if f not in sources:
            try:
                p = Path(f)
                sources[f] = p.read_text(encoding="utf-8", errors="replace").splitlines() if p.is_file() and p.stat().st_size < 2_000_000 else []
            except OSError:
                sources[f] = []
        n = int(d["line"])
        return _clip(sources[f][n - 1].strip(), 160) if 0 < n <= len(sources[f]) else ""

    def tidy(msg: str) -> str:
        msg = re.sub(r" and no accessible extension method .*$", "", msg)
        return _clip(re.sub(r"\s*\(are you missing [^)]*\)", "", msg), 300)

    out = [f"The build failed: {len(errors)} error{'s' if len(errors) != 1 else ''} (each listed once):"]
    for d in errors[:25]:
        where_at = f"{rel(d['file'])}:{d['line']}:{d['col']} " if d["line"] else ""
        out.append(f"  {where_at}{d['code']} {tidy(d['msg'])}")
        if src := source_line(d):
            out.append(f"      | {src}")
    if len(errors) > 25:
        out.append(f"  ... and {len(errors) - 25} more")
    hints = [h for c, h in _ERROR_HINTS.items() if any(d["code"] == c for d in errors)]
    out += [f"\n{h}" for h in hints]

    named = [d for d in errors if d["code"] in _API_ERRORS]
    proj = next((Path(d["proj"]) for d in errors if d.get("proj")), None)
    if proj is None:
        found = [q for ext in ("*.csproj", "*.fsproj", "*.vbproj") for q in where.glob(ext)]
        proj = found[0] if len(found) == 1 else None
    if named and proj is not None and proj.is_file():
        parts: list[str] = []
        why = ""
        try:
            search, refs = _project_refs_sync(proj)
            for mode, q, member in _api_queries(named):
                got = _lens_sync(search, refs, mode, q, member, 3500 if mode == "type" else 2200)
                if got and "Nothing by that name" not in got[:600]:
                    parts.append(got)
                if sum(map(len, parts)) > 9000:
                    break
        except (Fail, OSError, subprocess.TimeoutExpired) as e:
            why = f" (reading it failed: {_clip(str(getattr(e, 'result', e)), 200)})"
        if parts:
            out.append("\nThese errors name things that the library this project builds against doesn't have, as installed "
                       "(code written from memory of another version of it does this). Its real API, read from the installed files:\n")
            out.append("\n\n".join(parts))
            out.append(f'\nUse these names, not guesses. More: api_lookup(of="{proj}", type="Name") or find="word".')
        else:
            out.append(f'\nLook the real names up with api_lookup(of="{proj}", find="word") instead of guessing{why}.')
    if obsolete:
        out.append(f"\n({len(obsolete)} warnings say parts of the library used here are obsolete.)")
    return "\n".join(out), True


def _keep_whole(text: str) -> str:
    """Saves a whole output as an artifact:// link read_file can open ("" if the disk says no)."""
    try:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        aid = new_artifact_id()
        artifact_path(aid).write_text(text, encoding="utf-8", newline="")
        return f"artifact://{aid}"
    except OSError:
        return ""


@action("open_path", group="FILE", summary="open a file or folder in its app (or app=), or reveal it in Explorer",
        params="""
        path s the file, folder or http(s) address
        app s? open it in this app instead
        reveal b? true: show it selected in File Explorer
        expect s? what should show afterwards
        """, cost=2.0, star=True, expect=True, top="path", fallback='PowerShell("Start-Process ...")', timeout=20,
        risky=lambda a, c: _open_risky(a, c), limits="programs and scripts ask first; the default app may already be open")
async def open_path(ctx: Ctx, path: str, app: str = "", reveal: bool = False, **_) -> str:
    if re.match(r"(?i)https?://\S+$", path.strip()):
        # an address is something to open too (the user's own browser, not IO's tab), not a file named "http:"
        await asyncio.to_thread(os.startfile, path.strip())
        return ok(f"opened {path.strip()} in the default browser")
    p = _path(path)
    if not p.exists():
        raise Fail("NOT_FOUND", f"{p} doesn't exist", f'find_file("{p.name}")')
    before = {w.hwnd for w in windows(owned=True)}
    if reveal and p.is_dir() and not _named(ctx, "reveal", "select", "highlight", "where is", "where's", "in its folder", "containing folder"):
        reveal = False  # "File Explorer to my Downloads folder" means inside it, not Downloads selected in its parent
    if reveal:
        subprocess.Popen(["explorer.exe", f"/select,{p}"], creationflags=0x08000000)
        how = "Explorer /select"
    elif app:
        exe = _app_exe_path(app)
        if not exe:
            raise Fail("NOT_FOUND", f"can't find the app {app!r} to open it with", f'app_info("{app}") or open_path("{path}")')
        subprocess.Popen([exe, str(p)], creationflags=0x08000000)
        how = f"{Path(exe).name}"
    else:
        await asyncio.to_thread(os.startfile, str(p))
        how = "default app"
    stem = p.stem.lower() if p.is_file() else p.name.lower()
    w = await _wait_window(before, lambda x: stem[:20] in x.title.lower(), 8, any_new=True)
    if w is None:
        cands = [x for x in windows() if stem[:20] in x.title.lower()]
        if cands:
            await focus(cands[0])
            return ok(f"'{cands[0].title[:60]}' shows it (an already open window)", via=how)
        return unsure(f"opened {p.name} via {how} but no new window appeared within 8s", "list_windows()")
    ctx.opened.add(w.hwnd)
    front = await focus(w)
    return ok(f"opened {p.name} in '{w.title[:60]}'", via=how, now="front" if front else "behind")


# ======================================================================================================================
# L2 · PC: system facts and harmless controls
# ======================================================================================================================

CITY_ZONES = {
    "tokyo": "Tokyo Standard Time", "osaka": "Tokyo Standard Time", "japan": "Tokyo Standard Time", "seoul": "Korea Standard Time",
    "korea": "Korea Standard Time", "beijing": "China Standard Time", "shanghai": "China Standard Time", "china": "China Standard Time",
    "hong kong": "China Standard Time", "taipei": "Taipei Standard Time", "singapore": "Singapore Standard Time",
    "manila": "Singapore Standard Time", "kuala lumpur": "Singapore Standard Time", "sydney": "AUS Eastern Standard Time",
    "melbourne": "AUS Eastern Standard Time", "canberra": "AUS Eastern Standard Time", "brisbane": "E. Australia Standard Time",
    "perth": "W. Australia Standard Time", "adelaide": "Cen. Australia Standard Time", "auckland": "New Zealand Standard Time",
    "wellington": "New Zealand Standard Time", "new zealand": "New Zealand Standard Time", "delhi": "India Standard Time",
    "new delhi": "India Standard Time", "mumbai": "India Standard Time", "bangalore": "India Standard Time", "india": "India Standard Time",
    "kolkata": "India Standard Time", "dubai": "Arabian Standard Time", "abu dhabi": "Arabian Standard Time", "moscow": "Russian Standard Time",
    "istanbul": "Turkey Standard Time", "london": "GMT Standard Time", "uk": "GMT Standard Time", "dublin": "GMT Standard Time",
    "lisbon": "GMT Standard Time", "paris": "Romance Standard Time", "madrid": "Romance Standard Time", "brussels": "Romance Standard Time",
    "copenhagen": "Romance Standard Time", "berlin": "W. Europe Standard Time", "rome": "W. Europe Standard Time",
    "amsterdam": "W. Europe Standard Time", "vienna": "W. Europe Standard Time", "stockholm": "W. Europe Standard Time",
    "oslo": "W. Europe Standard Time", "zurich": "W. Europe Standard Time", "germany": "W. Europe Standard Time",
    "prague": "Central Europe Standard Time", "budapest": "Central Europe Standard Time", "warsaw": "Central European Standard Time",
    "athens": "GTB Standard Time", "bucharest": "GTB Standard Time", "helsinki": "FLE Standard Time", "kyiv": "FLE Standard Time",
    "kiev": "FLE Standard Time", "cairo": "Egypt Standard Time", "johannesburg": "South Africa Standard Time",
    "cape town": "South Africa Standard Time", "lagos": "W. Central Africa Standard Time", "nairobi": "E. Africa Standard Time",
    "new york": "Eastern Standard Time", "nyc": "Eastern Standard Time", "boston": "Eastern Standard Time", "washington": "Eastern Standard Time",
    "miami": "Eastern Standard Time", "atlanta": "Eastern Standard Time", "toronto": "Eastern Standard Time", "montreal": "Eastern Standard Time",
    "chicago": "Central Standard Time", "dallas": "Central Standard Time", "houston": "Central Standard Time", "denver": "Mountain Standard Time",
    "phoenix": "US Mountain Standard Time", "los angeles": "Pacific Standard Time", "la": "Pacific Standard Time",
    "san francisco": "Pacific Standard Time", "seattle": "Pacific Standard Time", "vancouver": "Pacific Standard Time",
    "las vegas": "Pacific Standard Time", "california": "Pacific Standard Time", "anchorage": "Alaskan Standard Time",
    "honolulu": "Hawaiian Standard Time", "hawaii": "Hawaiian Standard Time", "mexico city": "Central Standard Time (Mexico)",
    "sao paulo": "E. South America Standard Time", "rio de janeiro": "E. South America Standard Time",
    "buenos aires": "Argentina Standard Time", "bogota": "SA Pacific Standard Time", "lima": "SA Pacific Standard Time",
    "santiago": "Pacific SA Standard Time", "bangkok": "SE Asia Standard Time", "jakarta": "SE Asia Standard Time",
    "hanoi": "SE Asia Standard Time", "karachi": "Pakistan Standard Time", "dhaka": "Bangladesh Standard Time",
    "tehran": "Iran Standard Time", "jerusalem": "Israel Standard Time", "tel aviv": "Israel Standard Time", "riyadh": "Arab Standard Time",
    "utc": "UTC", "gmt": "UTC", "reykjavik": "Greenwich Standard Time", "halifax": "Atlantic Standard Time",
    "kathmandu": "Nepal Standard Time", "eastern": "Eastern Standard Time", "pacific": "Pacific Standard Time",
    "central": "Central Standard Time", "mountain": "Mountain Standard Time",
}


class _MEM(ctypes.Structure):
    _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD), ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class _PS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte), ("BatteryLifePercent", ctypes.c_byte),
                ("SystemStatusFlag", ctypes.c_byte), ("BatteryLifeTime", wt.DWORD), ("BatteryFullLifeTime", wt.DWORD)]


def _fixed_drives() -> list[str]:
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    out = []
    for i in range(26):
        if mask & (1 << i):
            d = f"{chr(65 + i)}:\\"
            if ctypes.windll.kernel32.GetDriveTypeW(d) == 3:  # DRIVE_FIXED
                out.append(d)
    return out


def _os_line() -> str:
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
        get = lambda n: str(winreg.QueryValueEx(k, n)[0]) if n else ""
        name, build = get("ProductName"), get("CurrentBuild")
        try:
            ver = get("DisplayVersion")
        except OSError:
            ver = ""
        try:
            ubr = get("UBR")
        except OSError:
            ubr = ""
    if build.isdigit() and int(build) >= 22000:
        name = name.replace("Windows 10", "Windows 11")  # the registry still says 10 on 11
    return f"{name} {ver} (build {build}{'.' + ubr if ubr else ''})"


def _cpu_name() -> str:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
    except OSError:
        return "unknown CPU"


@action("pc_info", group="PC", summary="a PC fact: time (any city), os, cpu, gpu, ram, disk, ip, wifi, uptime",
        params="""
        topic s time|date|os|cpu|gpu|ram|disk|battery|uptime|ip|wifi|displays|processes|network
        place s? city for time/date; "ram" sorts processes by memory
        """, cost=0.5, star=True, top="topic", fallback="PowerShell(command)", timeout=25)
async def pc_info(ctx: Ctx, topic: str, place: str = "", **_) -> str:
    now = dt.datetime.now()
    if topic in ("time", "date"):
        if not place or place.lower() in ("here", "local", "my", "my pc"):
            stamp = now.strftime("%I:%M %p, %A ").lstrip("0") + f"{now.day} " + now.strftime("%B %Y")
            return ok(stamp + f" (local, {time.tzname[time.localtime().tm_isdst > 0]})")
        key = place.lower().strip().removeprefix("in ").strip()
        zone = CITY_ZONES.get(key) or next((z for c, z in CITY_ZONES.items() if len(c) > 3 and c in key), "")
        if zone:
            cmd = f"[TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, {_q(zone)}).ToString('h:mm tt, dddd d MMMM yyyy')"
        else:  # any zone whose display name mentions the place
            cmd = (f"$z = [TimeZoneInfo]::GetSystemTimeZones() | Where-Object {{ $_.DisplayName -like {_q('*' + place + '*')} -or $_.Id -like "
                   f"{_q('*' + place + '*')} }} | Select-Object -First 1; if ($z) {{ $z.Id + '|' + [TimeZoneInfo]::ConvertTime([DateTime]::UtcNow, "
                   "[TimeZoneInfo]::Utc, $z).ToString('h:mm tt, dddd d MMMM yyyy') }")
        out, code = await ps(ctx, cmd, timeout=15)
        if not out or code:
            raise Fail("NOT_FOUND", f"no time zone known for {place!r}", 'pc_info("time", place="<a big city nearby>")')
        if "|" in out:
            zone, out = out.split("|", 1)
        return ok(f"{out.strip()} in {place} ({zone})")
    if topic == "os":
        return ok(await asyncio.to_thread(_os_line) + f", PC name {os.environ.get('COMPUTERNAME', '?')}, user {os.environ.get('USERNAME', '?')}")
    if topic == "cpu":
        out, _c = await ps(ctx, "$p = Get-CimInstance Win32_Processor | Select-Object -First 1; \"$($p.NumberOfCores)|$($p.LoadPercentage)\"", timeout=15)
        cores, load = (out.split("|") + ["", ""])[:2]
        return ok(f"{_cpu_name()}: {cores.strip() or '?'} cores, {os.cpu_count()} logical processors, load {load.strip() or '?'}%")
    if topic == "gpu":
        out = await asyncio.to_thread(run_quiet, ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                                                   "--format=csv,noheader"])
        if out.strip():
            lines = []
            for l in out.strip().splitlines():
                parts = [x.strip() for x in l.split(",")]
                if len(parts) >= 5:
                    lines.append(f"{parts[0]}: {parts[1]} busy, {parts[2]} of {parts[3]} memory used, {parts[4]}°C")
            return ok("; ".join(lines) or out.strip())
        out, _c = await ps(ctx, "(Get-CimInstance Win32_VideoController).Name -join '; '", timeout=15)
        return ok(out.strip() or "no GPU found")
    if topic == "ram":
        m = _MEM()
        m.dwLength = ctypes.sizeof(m)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        used = m.ullTotalPhys - m.ullAvailPhys
        return ok(f"{_human(m.ullTotalPhys)} RAM, {_human(used)} in use ({m.dwMemoryLoad}%), {_human(m.ullAvailPhys)} free")
    if topic == "disk":
        lines = []
        for d in _fixed_drives():
            try:
                u = shutil.disk_usage(d)
                lines.append(f"{d[:2]} {u.free / 1024 ** 3:.1f} GB free of {u.total / 1024 ** 3:.1f} GB ({u.used * 100 // u.total}% used)")
            except OSError:
                pass
        return ok("; ".join(lines) or "no fixed drives found")
    if topic == "battery":
        s = _PS()
        ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))
        if (s.BatteryFlag & 0xFF) == 128 or (s.BatteryFlag & 0xFF) == 255:
            return ok("no battery (a desktop PC on mains power)")
        return ok(f"battery {s.BatteryLifePercent & 0xFF}%, {'charging/plugged in' if s.ACLineStatus == 1 else 'on battery'}")
    if topic == "uptime":
        _k.GetTickCount64.restype = ctypes.c_ulonglong
        secs = _k.GetTickCount64() // 1000
        d, rem = divmod(int(secs), 86400)
        return ok(f"up {d} days {rem // 3600} h {rem % 3600 // 60} min (since {now - dt.timedelta(seconds=int(secs)):%Y-%m-%d %H:%M})")
    if topic == "ip":
        out, _c = await ps(ctx, "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254*' } | "
                                "ForEach-Object { $_.IPAddress + ' (' + $_.InterfaceAlias + ')' }", timeout=15)
        return ok("local IPv4: " + ("; ".join(l.strip() for l in out.splitlines() if l.strip()) or "none"))
    if topic == "wifi":
        out = await asyncio.to_thread(run_quiet, ["netsh", "wlan", "show", "interfaces"])
        ssid = re.search(r"^\s*SSID\s*:\s*(.+)$", out, re.M)
        sig = re.search(r"^\s*Signal\s*:\s*(.+)$", out, re.M)
        if not ssid:
            return ok("not on Wi-Fi (no wireless connection; probably wired)")
        return ok(f"Wi-Fi '{ssid.group(1).strip()}', signal {sig.group(1).strip() if sig else '?'}")
    if topic == "displays":
        mons = _monitors()
        return ok(f"{len(mons)} displays: " + "; ".join(f"#{i} {m[2] - m[0]}x{m[3] - m[1]} at ({m[0]},{m[1]}){' primary' if i == 1 else ''}"
                                                      for i, (m, _w2) in enumerate(mons, 1)))
    if topic == "processes":
        by = "WorkingSet64" if (place or "").lower() in ("ram", "memory", "mem") else "CPU"
        out, _c = await ps(ctx, f"Get-Process | Sort-Object {by} -Descending | Select-Object -First 10 | ForEach-Object "
                                "{ '{0} (pid {1}): {2:N0} MB, CPU {3:N0}s' -f $_.ProcessName, $_.Id, ($_.WorkingSet64 / 1MB), $_.CPU }", timeout=15)
        return ok(f"top processes by {'memory' if by != 'CPU' else 'CPU time'}:\n{out.strip()}")
    if topic == "network":
        out, _c = await ps(ctx, "Get-NetAdapter | Where-Object Status -eq 'Up' | ForEach-Object { $_.Name + ': ' + $_.InterfaceDescription + ', ' + $_.LinkSpeed }", timeout=15)
        return ok("connected adapters: " + ("; ".join(l.strip() for l in out.splitlines() if l.strip()) or "none"))
    raise Fail("BAD_ARGS", f"unknown topic {topic!r}", 'pc_info("time") or pc_info("disk")')


def _uninstall_entries() -> list[tuple[str, str, str]]:
    import winreg
    out = []
    for hive, path in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                       (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                       (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")):
        try:
            root = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, i)) as k:
                        vals = {}
                        for n in ("DisplayName", "InstallLocation", "DisplayVersion", "SystemComponent"):
                            try:
                                vals[n] = winreg.QueryValueEx(k, n)[0]
                            except OSError:
                                pass
                        if vals.get("DisplayName") and not vals.get("SystemComponent"):
                            out.append((str(vals["DisplayName"]), str(vals.get("InstallLocation") or ""), str(vals.get("DisplayVersion") or "")))
                except OSError:
                    continue
    return out


def _start_menu_names() -> list[tuple[str, str]]:
    roots = [Path(os.environ.get("ProgramData", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
             Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs"]
    out = []
    for r in roots:
        for dirpath, _d, files in os.walk(r, onerror=lambda e: None):
            for f in files:
                if f.lower().endswith((".lnk", ".url")):
                    out.append((f.rsplit(".", 1)[0], str(Path(dirpath) / f)))
    return out


def steam_games() -> list[str]:
    import winreg
    steam = ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = str(winreg.QueryValueEx(k, "SteamPath")[0])
    except OSError:
        steam = r"C:\Program Files (x86)\Steam"
    libs = {Path(steam)}
    try:
        vdf = (Path(steam) / "steamapps" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        libs |= {Path(p.replace("\\\\", "\\")) for p in re.findall(r'"path"\s+"([^"]+)"', vdf)}
    except OSError:
        pass
    names = []
    for lib in libs:
        for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
            try:
                m = re.search(r'"name"\s+"([^"]+)"', acf.read_text(encoding="utf-8", errors="replace"))
                if m and not re.search(r"Steamworks|Redistributable|Proton|Steam Linux Runtime", m.group(1)):
                    names.append(m.group(1))
            except OSError:
                continue
    return sorted(set(names), key=str.lower)


class _PE(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def processes() -> list[tuple[int, str]]:
    """(pid, exe name) of every process: one Toolhelp snapshot, no PowerShell."""
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.restype = wt.HANDLE
    snap = k.CreateToolhelp32Snapshot(0x2, 0)
    out = []
    pe = _PE()
    pe.dwSize = ctypes.sizeof(pe)
    try:
        ok_ = k.Process32FirstW(snap, ctypes.byref(pe))
        while ok_:
            out.append((pe.th32ProcessID, pe.szExeFile))
            ok_ = k.Process32NextW(snap, ctypes.byref(pe))
    finally:
        k.CloseHandle(snap)
    return out


def _mem_mb(pid: int) -> int:
    class PMC(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("a", ctypes.c_size_t), ("b", ctypes.c_size_t), ("c", ctypes.c_size_t), ("d", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    h = _k.OpenProcess(0x1000, False, pid)
    if not h:
        return 0
    try:
        pmc = PMC()
        pmc.cb = ctypes.sizeof(pmc)
        ctypes.windll.kernel32.K32GetProcessMemoryInfo(wt.HANDLE(h), ctypes.byref(pmc), pmc.cb)
        return int(pmc.WorkingSetSize // (1024 * 1024))
    finally:
        _k.CloseHandle(h)


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


APP_ALIASES = {  # what people call a game or app -> names it is installed under
    "runescape": ["jagex launcher", "runelite", "old school runescape", "osrs"], "osrs": ["runelite", "jagex launcher"],
    "minecraft": ["minecraft launcher"], "office": ["microsoft 365", "microsoft office"], "vscode": ["visual studio code"],
    "teams": ["microsoft teams"], "edge": ["microsoft edge"], "wow": ["world of warcraft", "battle.net"], "league": ["riot client", "league of legends"],
    "fortnite": ["epic games launcher"], "bluestacks": ["bluestacks"], "photoshop": ["adobe photoshop"],
}


def _installed_sync(name: str) -> dict:
    q = _squash(name)
    hits = []
    for disp, loc, ver in _uninstall_entries():
        if q and q in _squash(disp):
            hits.append(f"Uninstall entry '{disp}'" + (f" ({loc})" if loc else "") + (f", version {ver}" if ver else ""))
    for nm, path in _start_menu_names():
        if q and q in _squash(nm):
            hits.append(f"Start menu '{nm}'")
    progs = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs"
    if progs.exists():
        for d in progs.iterdir():
            if q and q in _squash(d.name):
                hits.append(f"folder {d}")
    for g in steam_games():
        if q and q in _squash(g):
            hits.append(f"Steam game '{g}'")
    running = []
    for pid, exe in processes():
        if q and q in _squash(exe.removesuffix(".exe")):
            running.append((pid, exe))
    titles = [w.title for w in windows() if q and q in _squash(w.title + w.exe)]
    return {"hits": list(dict.fromkeys(hits)), "running": running, "titles": titles}


@action("app_info", group="PC", summary="is an app installed / running, and its version; never launches it",
        params="""
        name s? the app (empty: list installed apps; "steam games")
        filter s? with an empty name: only apps containing this
        """, cost=1.0, star=True, top="name", fallback="PowerShell(command)", timeout=30,
        limits="checks uninstall entries, Start menu, Store apps, LocalAppData\\Programs and Steam")
async def app_info(ctx: Ctx, name: str = "", filter: str = "", **_) -> str:
    if not name or name.lower() in ("all", "installed", "apps"):
        apps = sorted({d for d, _l, _v in await asyncio.to_thread(_uninstall_entries)}, key=str.lower)
        if filter:
            apps = [a for a in apps if filter.lower() in a.lower()]
        return ok(f"{len(apps)} installed apps{' matching ' + repr(filter) if filter else ''}" + (" (first 80)" if len(apps) > 80 else "") + ":\n" +
                  "\n".join(apps[:80]))
    if re.fullmatch(r"(my )?steam( games| library)?", name.lower()):
        games = await asyncio.to_thread(steam_games)
        return ok(f"{len(games)} installed Steam games:\n" + "\n".join(games) if games else "no installed Steam games found")
    res = await asyncio.to_thread(_installed_sync, name)
    related = []
    for alias in APP_ALIASES.get(_squash(name), []) if not res["hits"] else []:  # RuneScape is installed as Jagex Launcher / RuneLite
        other = await asyncio.to_thread(_installed_sync, alias)
        related += other["hits"][:2]
        res["running"] += other["running"][:2]
    name = name.strip()
    if related:
        return ok(f"{name} isn't installed under that name, but these are (it is played/used through them): " +
                  "; ".join(list(dict.fromkeys(related))[:4]) + ("; one of them is running" if res["running"] else "; none running") + "; not launched")
    if not res["hits"] and not res["running"]:
        # Store apps and Start menu entries without a shortcut file
        out, _c = await ps(ctx, f"$n = {_q('*' + name + '*')}; (Get-StartApps | Where-Object Name -like $n).Name; "
                                "(Get-AppxPackage | Where-Object { $_.Name -like $n -and -not $_.IsFramework }) | ForEach-Object { 'Store: ' + $_.Name + ' ' + $_.Version }",
                           timeout=20)
        res["hits"] = [f"Start menu '{l.strip()}'" if not l.startswith("Store:") else l.strip() for l in out.splitlines() if l.strip()][:10]
    run = ""
    if res["running"]:
        pids = res["running"][:5]
        mem = [await asyncio.to_thread(_mem_mb, pid) for pid, _exe in pids]
        run = "running: " + ", ".join(f"{exe} (pid {pid}, {mb} MB)" for (pid, exe), mb in zip(pids, mem))
        if res["titles"]:
            run += "; windows: " + ", ".join(f"'{t[:40]}'" for t in res["titles"][:3])
    else:
        run = "not running"
    if not res["hits"]:
        # a command-line tool (dotnet, git, node, uv) often has no app entry at all: "dotnet doesn't look installed" sent
        # a build task looking for an SDK that was right there
        cli = shutil.which(name) if re.fullmatch(r"[\w.+-]+", name) else None
        if cli and "WindowsApps" not in cli:
            return ok(f"{name} is installed as a command-line tool: {cli} (on PATH; run_command(\"{name} --version\") gives its version); "
                      f"{run}; not launched")
        return ok(f"{name} doesn't look installed (no uninstall entry, Start menu item, Store app, program folder, Steam game or "
                  f"command on PATH); {run}; not launched")
    return ok(f"{name} is installed: " + "; ".join(res["hits"][:6]) + f"; {run}; not launched")


SETTINGS_PAGES = {
    "display": "display", "screen": "display", "resolution": "display", "scale": "display", "night light": "nightlight", "nightlight": "nightlight",
    "sound": "sound", "audio": "sound", "volume": "sound", "bluetooth": "bluetooth", "devices": "bluetooth", "printers": "printers",
    "mouse": "mousetouchpad", "touchpad": "devices-touchpad", "keyboard": "keyboard", "typing": "typing", "pen": "pen",
    "wifi": "network-wifi", "wi-fi": "network-wifi", "network": "network-status", "ethernet": "network-ethernet", "vpn": "network-vpn",
    "proxy": "network-proxy", "airplane": "network-airplanemode", "default apps": "defaultapps", "apps": "appsfeatures",
    "installed apps": "appsfeatures", "startup": "startupapps", "update": "windowsupdate", "windows update": "windowsupdate",
    "privacy": "privacy", "location": "privacy-location", "camera": "privacy-webcam", "microphone": "privacy-microphone",
    "background": "personalization-background", "wallpaper": "personalization-background", "colors": "colors", "dark mode": "colors",
    "themes": "themes", "lock screen": "lockscreen", "start": "personalization-start", "taskbar": "taskbar", "fonts": "fonts",
    "power": "powersleep", "sleep": "powersleep", "battery": "batterysaver", "storage": "storagesense", "about": "about",
    "date": "dateandtime", "time": "dateandtime", "language": "regionlanguage", "region": "regionformatting", "notifications": "notifications",
    "focus": "quiethours", "multitasking": "multitasking", "clipboard": "clipboard", "accounts": "yourinfo", "sign-in": "signinoptions",
    "gaming": "gaming-gamebar", "game mode": "gaming-gamemode", "accessibility": "easeofaccess", "mouse pointer": "easeofaccess-mousepointer",
    "graphics": "display-advancedgraphics", "home": "", "settings": "",
}


@action("open_settings", group="PC", summary="open a Windows Settings page (display, sound, bluetooth, wifi, ...)",
        params="""
        page s display, sound, bluetooth, wifi, default apps, update...
        expect s? what should show afterwards
        """, cost=1.5, star=True, expect=True, top="page", fallback='PowerShell("Start-Process ms-settings:...")', timeout=20,
        limits="opens the page only; change values with set_control")
async def open_settings(ctx: Ctx, page: str, **_) -> str:
    key = page.lower().strip().removesuffix(" settings").strip()
    uri = SETTINGS_PAGES.get(key)
    if uri is None:
        uri = next((v for k, v in SETTINGS_PAGES.items() if k in key and len(k) > 2), None)
    if uri is None:
        if re.fullmatch(r"[a-z0-9-]+", key):
            uri = key  # already an ms-settings name
        else:
            raise Fail("NOT_FOUND", f"no Settings page known for {page!r}", 'open_settings("display") or open_settings("home")')
    before = {w.hwnd for w in windows(owned=True)}
    await asyncio.to_thread(os.startfile, f"ms-settings:{uri}")
    w = None
    t_end = time.time() + 8
    while time.time() < t_end and w is None:
        await asyncio.sleep(0.3)
        w = next((x for x in windows() if x.title == "Settings" or x.exe == "systemsettings.exe"), None)
    if w is None:
        raise Fail("TIMEOUT", "Settings didn't open within 8s", "list_windows()")
    if w.hwnd not in before:
        ctx.opened.add(w.hwnd)
    await focus(w)
    await asyncio.sleep(0.6)
    words = [x for x in re.findall(r"[a-z]{3,}", key) if x not in ("settings", "the")]
    try:
        text = await on_uia(_texts_sync, w.hwnd, timeout=4.0)
    except Exception:
        text = ""
    if words and not any(x in text for x in words):
        return unsure(f"Settings is open (ms-settings:{uri}) but its page doesn't mention {page!r} yet", 'read_window("Settings")')
    return ok(f"Settings is open at {page} (ms-settings:{uri})", now="front")


# --- calc: a safe evaluator (no eval) ---

_FUNCS = {"sqrt": math.sqrt, "round": round, "abs": abs, "min": min, "max": max, "log": math.log10, "ln": math.log, "log10": math.log10,
          "log2": math.log2, "sin": lambda d: math.sin(math.radians(d)), "cos": lambda d: math.cos(math.radians(d)),
          "tan": lambda d: math.tan(math.radians(d)), "floor": math.floor, "ceil": math.ceil, "exp": math.exp, "factorial": math.factorial}
_CONSTS = {"pi": math.pi, "e": math.e}
_OPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
        ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b, ast.Pow: lambda a, b: a ** b}
UNITS = {  # to a base unit
    "km": ("len", 1000), "kilometers": ("len", 1000), "kilometres": ("len", 1000), "m": ("len", 1), "meters": ("len", 1), "metres": ("len", 1),
    "cm": ("len", 0.01), "mm": ("len", 0.001), "mi": ("len", 1609.344), "miles": ("len", 1609.344), "mile": ("len", 1609.344),
    "ft": ("len", 0.3048), "feet": ("len", 0.3048), "foot": ("len", 0.3048), "in": ("len", 0.0254), "inches": ("len", 0.0254),
    "inch": ("len", 0.0254), "yd": ("len", 0.9144), "yards": ("len", 0.9144),
    "kg": ("mass", 1), "kilograms": ("mass", 1), "g": ("mass", 0.001), "grams": ("mass", 0.001), "lb": ("mass", 0.45359237),
    "lbs": ("mass", 0.45359237), "pounds": ("mass", 0.45359237), "oz": ("mass", 0.028349523125), "ounces": ("mass", 0.028349523125),
    "tb": ("data", 1024 ** 4), "gb": ("data", 1024 ** 3), "mb": ("data", 1024 ** 2), "kb": ("data", 1024), "bytes": ("data", 1), "b": ("data", 1),
    "hours": ("time", 3600), "hour": ("time", 3600), "h": ("time", 3600), "minutes": ("time", 60), "min": ("time", 60), "seconds": ("time", 1),
    "s": ("time", 1), "days": ("time", 86400), "day": ("time", 86400), "weeks": ("time", 604800),
    "l": ("vol", 1), "liters": ("vol", 1), "litres": ("vol", 1), "ml": ("vol", 0.001), "gallons": ("vol", 3.785411784), "gal": ("vol", 3.785411784),
}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        a, b = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(b) > 1000:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](a, b)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _eval(node.operand)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
        args = [_eval(a) for a in node.args]
        if node.func.id == "factorial" and (args[0] > 500 or args[0] != int(args[0])):
            raise ValueError("factorial too large")
        return _FUNCS[node.func.id](*args)
    raise ValueError(f"unsupported: {ast.dump(node)[:60]}")


def _fmt(v) -> str:
    if isinstance(v, float):
        if v.is_integer() and abs(v) < 1e15:
            v = int(v)
        else:
            s = f"{v:.10g}"
            return s + (f" ({v:,.4f})" if abs(v) >= 10000 else "")
    return f"{v} ({v:,})" if isinstance(v, int) and abs(v) >= 10000 else str(v)


def _date(s: str) -> dt.date:
    s = s.strip().lower()
    if s in ("today", "now"):
        return dt.date.today()
    if s == "tomorrow":
        return dt.date.today() + dt.timedelta(days=1)
    if s == "yesterday":
        return dt.date.today() - dt.timedelta(days=1)
    for f in ("%Y-%m-%d", "%d %B %Y", "%B %d %Y", "%B %d, %Y", "%d %b %Y", "%b %d %Y", "%b %d, %Y", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, f).date()
        except ValueError:
            continue
    raise ValueError(f"unreadable date {s!r}")


def calculate(expr: str) -> str:
    """The answer text, or raises ValueError. Percentages, words, dates and units on top of plain arithmetic."""
    s = expr.strip().rstrip("?=. ").lower()
    s = re.sub(r"^(what is|what's|calculate|compute|how much is|convert)\s+", "", s)
    m = re.match(r"(?:number of )?days (?:between|from) (.+?) (?:and|to|until) (.+)$", s)
    if m:
        a, b = _date(m.group(1)), _date(m.group(2))
        return f"{abs((b - a).days):,} days between {a} and {b}"
    m = re.match(r"(?:the )?(?:date )?(\d+) (day|week|month)s? (from|after|before|ago)(?: (.+))?$", s)
    if m:
        n, unit, way = int(m.group(1)), m.group(2), m.group(3)
        base = _date(m.group(4)) if m.group(4) else dt.date.today()
        days = n * (7 if unit == "week" else 30 if unit == "month" else 1)
        d = base - dt.timedelta(days=days) if way in ("before", "ago") else base + dt.timedelta(days=days)
        return f"{d:%A %d %B %Y}" + (" (months counted as 30 days)" if unit == "month" else "")
    m = re.match(r"(-?[\d.,]+)\s*°?\s*(c|f|celsius|fahrenheit)\s+(?:in|to)\s+°?\s*(c|f|celsius|fahrenheit)$", s)
    if m:
        v = float(m.group(1).replace(",", ""))
        src, dst = m.group(2)[0], m.group(3)[0]
        out = v if src == dst else (v * 9 / 5 + 32 if src == "c" else (v - 32) * 5 / 9)
        return f"{v:g}°{src.upper()} = {out:.2f}°{dst.upper()}"
    m = re.match(r"(-?[\d.,]+)\s*([a-z]+)\s+(?:in|to|into)\s+([a-z]+)$", s)
    if m and m.group(2) in UNITS and m.group(3) in UNITS:
        (k1, f1), (k2, f2) = UNITS[m.group(2)], UNITS[m.group(3)]
        if k1 != k2:
            raise ValueError(f"can't convert {m.group(2)} to {m.group(3)}")
        v = float(m.group(1).replace(",", ""))
        return f"{v:g} {m.group(2)} = {_fmt(round(v * f1 / f2, 6))} {m.group(3)}"
    s = re.sub(r"(?<=\d),(?=\d{3}\b)", "", s)  # thousands separators, not function argument commas
    s = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s*", r"(\1/100)*", s)
    s = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", s)
    for word, op in ((r"\bmultiplied by\b|\btimes\b|\bx\b|×", "*"), (r"\bdivided by\b|\bover\b|÷", "/"), (r"\bplus\b|\band\b", "+"),
                     (r"\bminus\b|\bless\b", "-"), (r"\bto the power of\b|\^", "**"), (r"\bsquared\b", "**2"), (r"\bcubed\b", "**3"),
                     (r"\bmod(?:ulo)?\b", "%"), (r"\bsquare root of\b|\bsqrt of\b", "sqrt ")):
        s = re.sub(word, op, s)
    s = re.sub(r"\bsqrt\s+(\d+(?:\.\d+)?)", r"sqrt(\1)", s)
    if not re.fullmatch(r"[\d\s.+\-*/%()a-z,_]+", s):
        raise ValueError(f"not a calculation: {expr!r}")
    value = _eval(ast.parse(s, mode="eval"))
    return f"{expr.strip().rstrip('?= ')} = {_fmt(value)}"


@action("calc", group="PC", summary="exact maths: arithmetic, % of, dates (days between), unit conversion",
        params="expr s e.g. 17% of 2340, days between 2026-01-01 and today, 5 km in miles",
        cost=0.01, star=True, top="expr", fallback="PowerShell(\"[math]::...\")", limits="never opens Calculator (calculator(expression) does)")
async def calc(ctx: Ctx, expr: str, **_) -> str:
    try:
        return ok(calculate(expr))
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, TypeError) as e:
        raise Fail("BAD_ARGS", f"can't calculate {expr!r}: {e}", 'calc("1234 * 5678") or calc("17% of 2340")')


MEDIA = {"play_pause": 0xB3, "next": 0xB0, "prev": 0xB1, "vol_up": 0xAF, "vol_down": 0xAE, "mute": 0xAD}


@action("media_key", group="PC", summary="press a media key: play/pause, next, previous, volume up/down, mute",
        params="""
        key s play_pause|next|prev|vol_up|vol_down|mute
        times i? how many presses (volume steps are 2%)
        """, cost=0.05, star=True, top="key", fallback='Shortcut("volume_up")', limits="no read-back: says sent, not the new volume")
async def media_key(ctx: Ctx, key: str, times: int = 1, **_) -> str:
    vk = MEDIA[key]
    n = builtins_max(1, min(int(times or 1), 50))
    for _i in range(n):
        _send([_key_input(vk), _key_input(vk, flags=2)])
        await asyncio.sleep(0.03)
    return ok(f"sent {key} x{n}")


@action("screenshot", group="PC", summary="save a PNG of a window or display; returns the file path",
        params="""
        window s? part of the window title
        display i? display number (0 = primary)
        path s? where to save (default Pictures\\Screenshots)
        """, cost=0.4, top="window?", fallback="look_at_screen(question)")
async def screenshot(ctx: Ctx, window: str = "", display: int = 0, path: str = "", **_) -> str:
    w = resolve(ctx, window) if window else None
    if window and w is None:
        raise Fail("NOT_FOUND", f"no window matching {window!r}", "list_windows()")
    if w is not None:
        guard_read(ctx, w)
        await focus(w)
        await asyncio.sleep(0.3)
    rect = _rect(w.hwnd) if w else _area_rect(ctx, None, int(display or 0))
    p = _path(path) if path else known_folder("pictures") / "Screenshots" / f"IO-{dt.datetime.now():%Y%m%d-%H%M%S}.png"
    if p.suffix.lower() != ".png":
        p = p / f"IO-{dt.datetime.now():%Y%m%d-%H%M%S}.png" if not p.suffix else p.with_suffix(".png")
    writable(ctx, p)

    def save() -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        grab(rect).save(p, format="PNG")

    await asyncio.to_thread(save)
    return ok(f"saved {p} ({_size(p)}, {rect[2] - rect[0]}x{rect[3] - rect[1]})")


# ======================================================================================================================
# L2 · WEB: IO's own browser tab only
# ======================================================================================================================

REF_LINE = re.compile(r'^\s*-\s+([a-z]+)(?:\s+"((?:[^"\\]|\\.)*)")?([^\n]*?)\[ref=([^\]\s]+)\]([^\n]*)', re.M)
BLOCKED_WEB = re.compile(r"\b(ad|ads|sponsored|buy|buy now|purchase|checkout|check out|pay|place order|subscribe|sign in|log in|login|sign up|"
                         r"register|download)\b", re.I)
SKIP_RESULTS = re.compile(r"youtube\.com|youtu\.be|tiktok\.com|facebook\.com|instagram\.com|twitter\.com|x\.com/|pinterest\.|amazon\.|ebay\.", re.I)
PAGE_STATE_JS = "() => location.href + '\\n' + document.title + '\\n' + (document.body ? document.body.innerText.slice(0, 600) : '')"
MAIN_TEXT_JS = ("() => { const b = document.body ? document.body.innerText : ''; "
                "const m = document.querySelector('#mw-content-text, main, [role=main], article'); "
                "const t = m ? m.innerText : ''; return t.trim().length > 200 ? t : b; }")
LINKS_ALL_JS = ("() => { const main = document.querySelector('#mw-content-text, article, main') || document.body; "
                "const seen = new Set(); const out = []; for (const root of [main, document.body]) { for (const a of root.querySelectorAll('a[href]')) { "
                "const t = (a.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 100); if (!t || !a.href.startsWith('http') || seen.has(a.href)) continue; "
                "seen.add(a.href); out.push([t, a.href]); if (out.length >= 300) return out; } } return out; }")
TABLES_JS = ("() => [...document.querySelectorAll('table')].slice(0, 5).map(t => [...t.rows].slice(0, 60).map(r => "
             "[...r.cells].map(c => c.innerText.trim().replace(/\\s+/g, ' ').slice(0, 80)).join('\\t')).join('\\n'))")
HEADINGS_JS = "() => [...document.querySelectorAll('h1, h2, h3')].map(h => h.tagName + ' ' + h.innerText.trim().replace(/\\s+/g, ' ')).slice(0, 80)"


def _need_browser(ctx: Ctx) -> Any:
    if ctx.browser is None:
        raise Fail("NEEDS", "no browser tools this task", "research(question)")
    if ctx.constraints and any(k == "no_browser" for k, _t in ctx.constraints):
        raise Fail("BLOCKED", "the user said not to use the browser", "answer from what you know, or ask_user")
    return ctx.browser


async def bcall(ctx: Ctx, tool: str, args: dict, timeout: float = 25) -> str:
    res = await asyncio.wait_for(_need_browser(ctx).call_tool(tool, args), timeout)
    return _h().text_of(res)


async def beval(ctx: Ctx, js: str, timeout: float = 15) -> str:
    raw = await bcall(ctx, "browser_evaluate", {"function": js}, timeout)
    if raw.startswith("error"):
        raise Fail("UNSUPPORTED", f"the page didn't answer: {raw[6:120]}", "read_page(url) again")
    return _h().page_text(raw)


def _json(text: str):
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"### Result\s*\n(.*?)(?:\n###|$)", text, re.S)
        return json.loads(m.group(1)) if m else None


async def page_yaml(ctx: Ctx) -> str:
    h = _h()
    res = await bcall(ctx, "browser_snapshot", {})
    return h.inline_browser_snapshot(res, h.HERE / "data" / "browser")


def refs(yaml: str) -> list[dict]:
    """Snapshot lines like `- link "More information..." [ref=e12]` as {role, name, ref, rest}."""
    return [{"role": m.group(1), "name": (m.group(2) or "").replace('\\"', '"'), "ref": m.group(4), "rest": (m.group(3) + m.group(5))}
            for m in REF_LINE.finditer(yaml)]


def _pick(items: list[dict], text: str, roles: set | None) -> list[dict]:
    t = _norm(text)
    pool = [x for x in items if (not roles or x["role"] in roles) and x["name"]]
    for test in (lambda n: n == t, lambda n: n.startswith(t), lambda n: len(t) >= 3 and t in n):
        hit = [x for x in pool if test(_norm(x["name"]))]
        if hit:
            return hit
    return []


async def _navigate(ctx: Ctx, url: str) -> str:
    if not re.match(r"^[a-z]+:", url, re.I):
        url = "https://" + url.lstrip("/")
    if not re.match(r"^https?://", url, re.I):  # file:, chrome:, javascript: ... are not web pages
        raise Fail("BLOCKED", "only http and https pages", "a web address")
    res = await bcall(ctx, "browser_navigate", {"url": url}, 40)
    if not res.startswith("error"):
        ctx.tab_open = True
    return res


async def _google(ctx: Ctx, query: str) -> list:
    """Google results as [title, url, snippet] rows. Always Google; a consent or captcha page is never solved."""
    res = await _navigate(ctx, "https://www.google.com/search?q=" + urllib.parse.quote_plus(query))
    if res.startswith("error"):
        raise Fail("UNSUPPORTED", f"the browser couldn't open Google: {res[6:120]}", f'research("{query[:60]}")')
    state = await beval(ctx, PAGE_STATE_JS)
    if re.search(r"/sorry/|unusual traffic|before you continue|not a robot|captcha", state, re.I):
        raise Fail("BLOCKED", "Google asks for a check", f'research("{query[:60]}")')
    try:
        rows = _json(await beval(ctx, _h().RESULTS_JS)) or []
    except Exception:
        rows = []
    return [r for r in rows if isinstance(r, list) and len(r) >= 3]


@action("web_search", group="WEB", summary="Google search in IO's tab: the top results with their addresses",
        params="query s what to search for", cost=3.0, tier="web", star=True, top="query", fallback="browser_open(google url)",
        modes=frozenset({"single", "director", "local"}), timeout=60, limits="a captcha page is never solved (use research)")
async def web_search(ctx: Ctx, query: str, **_) -> str:
    rows = await _google(ctx, query)
    if not rows:
        raise Fail("NOT_FOUND", f"Google showed no results for {query!r}", f'research("{query[:60]}") or web_search with other words')
    lines = [f"{i}. {t or '(no title)'}\n   {u}\n   {s[:220]}" for i, (t, u, s) in enumerate(rows[:8], 1)]
    return ok(f"Google results for {query!r} (answer from these, or read_page(url) the best one):\n" + "\n".join(lines))


_TEXT_FILE = re.compile(r"\.(txt|md|json|csv|xml|ya?ml|toml|ini|cfg|cs|csproj|props|targets|sln|py|js|mjs|ts|tsx|jsx|java|go|rs|c|h|"
                        r"cpp|hpp|rb|php|sh|ps1|bat|kt|swift|lua|sql|log|patch|diff)$", re.I)


def _raw_url(url: str) -> str | None:
    """The address of a plain-text file, or None for a web page. GitHub's file pages become their raw files. In the
    tab, raw.githubusercontent.com pages timed out on every read (five in one task), and a GitHub file page is mostly
    menus around the code."""
    u = url.strip() if "://" in url else "https://" + url.strip()
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/blob/(.+?)(?:[?#].*)?$", u)
    if m:
        return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}"
    parts = urllib.parse.urlparse(u)
    if parts.netloc.lower() in ("raw.githubusercontent.com", "gist.githubusercontent.com") or _TEXT_FILE.search(parts.path):
        return u
    return None


def _fetch_text_sync(url: str) -> str:
    import urllib.request  # noqa: PLC0415
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) IO"})
    with urllib.request.urlopen(req, timeout=20) as r:
        if "html" in (r.headers.get("Content-Type") or "").lower():
            raise ValueError("a web page, not a text file")
        return r.read(3_000_000).decode("utf-8", "replace")


@action("read_page", group="WEB", summary="read a page in IO's tab: text (find= for facts), links, tables",
        params="""
        url s? the address (empty: the page already open)
        find s? only the lines about these words
        what s? text|links|tables|headings
        """, cost=2.5, tier="web", star=True, top="url?,find?", fallback="browser_open(url) then browser_read(find)",
        modes=frozenset({"single", "director", "local"}), timeout=60, limits="IO's tab only, never the user's own tabs")
async def read_page(ctx: Ctx, url: str = "", find: str = "", what: str = "text", **_) -> str:
    h = _h()
    raw = _raw_url(url) if url and what == "text" else None
    if raw:  # a plain-text file (source code on GitHub, a .json, a .md): fetched directly, not through the tab
        try:
            text = await asyncio.to_thread(_fetch_text_sync, raw)
            n = ctx.page_chars or 6000
            body = h.find_in_text(text, find) if find else text[:n] + (
                f"\n[{len(text) - n:,} more characters; use find= to look for something]" if len(text) > n else "")
            return ok(f"{raw} (a text file, read directly):\n{body}", via="http")
        except (OSError, ValueError):
            pass  # not there, or a web page after all: the tab tries it
    _need_browser(ctx)
    if url:
        res = await _navigate(ctx, url)
        if res.startswith("error"):
            if ctx.win is not None:  # Windows-MCP's plain fetch, for when the tab can't load it
                raw = h.text_of(await asyncio.wait_for(ctx.win.call_tool("Scrape", {"url": url if "://" in url else "https://" + url}), 40))
                if not raw.startswith("error"):
                    body = h.find_in_text(raw, find) if find else raw[:6000]
                    return ok(f"{url} (fetched without the tab):\n{body}", via="Scrape")
            raise Fail("UNSUPPORTED", f"couldn't open {url}: {res[6:150]}", f'web_search("{url[:60]}")')
    elif not ctx.tab_open:
        raise Fail("NEEDS", "IO has no page open in its tab yet", 'read_page(url="https://...") or web_search(query)')
    head = (await beval(ctx, "() => document.title + ' — ' + location.href")).strip()
    if what == "links":
        links = _json(await beval(ctx, LINKS_ALL_JS)) or []
        if find:
            words = [x for x in re.findall(r"\w{3,}", find.lower())]
            links = sorted(links, key=lambda l: -sum(x in l[0].lower() or x in l[1].lower() for x in words))
        return ok(f"{head}\n" + "\n".join(f"- {t} -> {u}" for t, u in links[:60]))
    if what == "tables":
        tables = _json(await beval(ctx, TABLES_JS)) or []
        if not tables:
            raise Fail("NOT_FOUND", "the page has no tables", 'read_page(find="...")')
        return ok(f"{head}\n" + "\n\n".join(f"table {i}:\n{t[:2500]}" for i, t in enumerate(tables, 1))[:8000])
    if what == "headings":
        heads = _json(await beval(ctx, HEADINGS_JS)) or []
        return ok(f"{head}\n" + ("\n".join(heads) if heads else "(the page has no h1-h3 headings)"))
    # without find=, the page's main content: sites put their menus first (python.org's ran thousands of characters
    # before a release's date), and a reading that is mostly menu crowds out the facts
    text = await beval(ctx, MAIN_TEXT_JS if not find else "() => document.body ? document.body.innerText : ''", 20)
    if not text.strip():
        return unsure(f"{head} has no readable text (still loading, or all images)", 'wait_until("screen_still") then read_page()')
    n = ctx.page_chars or 6000
    body = h.find_in_text(text, find) if find else text[:n] + ("\n[page continues; use find= to look for something]" if len(text) > n else "")
    hint = await h.meanings_hint(ctx.browser, text, ctx.request or find)
    return ok(f"{head}\n{hint or body}")


@action("web_click", group="WEB", summary="click a link or button in IO's tab by its text; checks the page",
        params="""
        text s the link or button text
        kind s? link|button|any
        """, cost=2.0, tier="web", star=True, top="text", fallback="browser_snapshot() then browser_click(ref)",
        modes=frozenset({"single", "director", "local"}), timeout=45,
        limits="never ads, buy, pay, subscribe, sign-in or download links unless the request says so")
async def web_click(ctx: Ctx, text: str, kind: str = "any", **_) -> str:
    _need_browser(ctx)
    if not ctx.tab_open:
        raise Fail("NEEDS", "IO has no page open in its tab yet", 'read_page(url="https://...")')
    if BLOCKED_WEB.search(text) and not _named(ctx, *BLOCKED_WEB.findall(text)):
        raise Fail("BLOCKED", f"IO doesn't click {text!r} unless asked", "read_page() for another link")
    roles = {"link": {"link"}, "button": {"button"}}.get(kind, {"link", "button", "menuitem", "tab", "checkbox", "radio", "option", "treeitem"})
    yaml = await page_yaml(ctx)
    hits = _pick(refs(yaml), text, roles)
    if not hits:
        names = [x["name"] for x in refs(yaml) if x["role"] in roles and x["name"]]
        near = difflib.get_close_matches(text, names, n=4, cutoff=0.4)
        raise Fail("NOT_FOUND", f"no {kind if kind != 'any' else 'link or button'} {text!r} on the page" + (f"; closest: {', '.join(near)}" if near else ""),
                   'read_page(what="links")')
    if len({x["name"] for x in hits}) > 1 and len(hits) > 1 and _norm(hits[0]["name"]) != _norm(text):
        raise Fail("AMBIGUOUS", f"{len(hits)} match: " + "; ".join(f"{x['role']} \"{x['name'][:40]}\"" for x in hits[:5]), f'web_click("{hits[0]["name"][:40]}")')
    target = hits[0]
    if BLOCKED_WEB.search(target["name"]) and not _named(ctx, *BLOCKED_WEB.findall(target["name"])):
        raise Fail("BLOCKED", f"IO doesn't click {target['name']!r} unless asked", "read_page() for another link")
    before = (await beval(ctx, "() => location.href")).strip()
    res = await bcall(ctx, "browser_click", {"element": target["name"][:80] or target["role"], "target": target["ref"], "ref": target["ref"]}, 30)
    if res.startswith("error"):
        raise Fail("UNSUPPORTED", f"the click failed: {res[6:150]}", "browser_snapshot() then browser_click(ref)")
    await asyncio.sleep(0.6)
    after = (await beval(ctx, "() => location.href + ' | ' + document.title")).strip()
    if not after.startswith(before + " |"):
        return ok(f"clicked {target['role']} \"{target['name'][:50]}\"", now=after[:200])
    if hashlib.sha1((await page_yaml(ctx)).encode()).digest() != hashlib.sha1(yaml.encode()).digest():
        return ok(f"clicked {target['role']} \"{target['name'][:50]}\" (the page changed)", now=after[:200])
    return unsure(f"clicked {target['role']} \"{target['name'][:50]}\" but the page looks the same", 'read_page() or web_click with another text')


@action("web_fill", group="WEB", summary="fill form fields in IO's tab by label; submit=true presses the button",
        params="""
        fields o {"label": "value", ...}
        submit b? press Enter/submit afterwards
        """, cost=2.5, tier="web", star=True, top="fields", fallback="browser_type(ref, text)",
        modes=frozenset({"single", "director", "local"}), timeout=60, risky=lambda a, c: _submit_risky(a, c),
        limits="never passwords, payment or ID fields")
async def web_fill(ctx: Ctx, fields: dict, submit: bool = False, **_) -> str:
    _need_browser(ctx)
    if isinstance(fields, dict) and fields and all(ADDRESS_FIELD.match(str(k).strip()) for k in fields):
        # {"address bar": "example.com"}: small models "type" a URL to go to a page; that is a navigation, not a form
        url = str(next(iter(fields.values()))).strip()
        url = url if re.match(r"^[a-z]+://", url, re.I) else "https://" + url
        res = await _navigate(ctx, url)
        if res.startswith("error"):
            raise Fail("NOT_FOUND", f"couldn't open {url}: {res[6:120]}", f'read_page("{url}")')
        state = await beval(ctx, PAGE_STATE_JS)
        lines = state.splitlines()
        return ok(f"opened {lines[0][:150] if lines else url}" + (f" — '{lines[1][:80]}'" if len(lines) > 1 else ""), via="navigate",
                  now="use web_click(text) for its links, read_page() for its text")
    if not ctx.tab_open:
        raise Fail("NEEDS", "IO has no page open in its tab yet", 'read_page(url="https://...")')
    if not isinstance(fields, dict) or not fields:
        raise Fail("BAD_ARGS", 'fields is {"label": "value"}', 'web_fill({"Search": "weather"})')
    items = refs(await page_yaml(ctx))
    filled, missing, last = [], [], None
    for label, value in fields.items():
        value = str(value)
        if CRED.search(label) or _card_like(value):
            raise Fail("BLOCKED", f"\"{label}\" is a password, payment or ID field", "ask_user (the user fills it in)")
        hit = _pick(items, label, {"textbox", "searchbox", "combobox", "spinbutton", "checkbox", "radio", "slider"})
        if not hit:
            missing.append(label)
            continue
        x = hit[0]
        el = {"element": x["name"][:80] or label, "target": x["ref"], "ref": x["ref"]}
        if x["role"] in ("textbox", "searchbox", "spinbutton"):
            res = await bcall(ctx, "browser_type", {**el, "text": value}, 30)
            last = x
        elif x["role"] == "combobox":
            res = await bcall(ctx, "browser_select_option", {**el, "values": [value]}, 30)
            if res.startswith("error"):  # an editable combobox: type into it
                res = await bcall(ctx, "browser_type", {**el, "text": value}, 30)
        else:
            checked = "[checked]" in x["rest"] or "checked=true" in x["rest"]
            want = value.strip().lower() in ON
            res = "" if checked == want else await bcall(ctx, "browser_click", el, 30)
        if res.startswith("error"):
            missing.append(f"{label} ({res[6:60]})")
            continue
        try:
            got = _h().page_text(await bcall(ctx, "browser_evaluate", {"function": "(el) => el.value ?? String(el.checked)", **el}, 10))
        except Exception:
            got = value
        filled.append(f"{label}={str(got).strip()[:40]!r}")
    if submit and filled:
        await bcall(ctx, "browser_press_key", {"key": "Enter"}, 20)
        await asyncio.sleep(0.8)
    if not filled:
        raise Fail("NOT_FOUND", f"no fields matched: {', '.join(missing)}", 'browser_snapshot() to see the field names')
    text = f"filled {', '.join(filled)}" + (" and submitted" if submit else "")
    if missing:
        return unsure(f"{text}; not found: {', '.join(missing)}", "browser_snapshot() to see the field names")
    now = (await beval(ctx, "() => location.href")).strip() if submit else ""
    return ok(text, now=now[:150])


ADDRESS_FIELD = re.compile(r"^(the\s+)?(address(\s+bar)?|url(\s+bar)?|location(\s+bar)?|omnibox|web\s*address|website|site)$", re.I)


def _submit_risky(args: dict, ctx: Ctx) -> str:
    fields = args.get("fields")
    if isinstance(fields, dict) and fields and all(ADDRESS_FIELD.match(str(k).strip()) for k in fields):
        return ""  # going to an address isn't submitting a form
    if args.get("submit") and not re.search(r"\b(submit|search|send|look up|google|find)\b", ctx.request or "", re.I):
        return "submit a web form: " + json.dumps(args.get("fields"), ensure_ascii=False)[:150]
    return ""


@action("web_close_tab", group="WEB", summary="close IO's own browser tab (end of task)", params="", cost=1.0, tier="web",
        modes=frozenset({"internal"}), timeout=10, limits="internal: closing the last controlled tab mid-task drops the browser connection")
async def web_close_tab(ctx: Ctx, **_) -> str:
    if ctx.browser is None or not ctx.tab_open:
        return ok("no IO tab open")
    try:
        if ctx.browser_mode == "chrome":  # the extension closes the tab when it lands on DONE_URL
            await asyncio.wait_for(ctx.browser.call_tool("browser_navigate", {"url": getattr(_h(), "DONE_URL", DONE_URL_DEFAULT)}), 4)
        else:
            await asyncio.wait_for(ctx.browser.call_tool("browser_close", {}), 4)
    except Exception as e:
        ctx.tab_open = False
        return unsure(f"tried to close IO's tab: {type(e).__name__}")
    ctx.tab_open = False
    return ok("closed IO's tab")


# ======================================================================================================================
# L3 · DO: task skills (fixed scripts over L2 actions; a failure says at which step)
# ======================================================================================================================

async def _step(steps: list, name: str, coro) -> str:
    """Runs one L2 step; a failure becomes 'at <step>: <error>' plus the steps that worked, so the decider resumes there."""
    try:
        res = await coro
    except Fail as e:
        raise Fail(e.result.split(":")[1], f"at {name}: {e.result.split(': ', 1)[1]}" + (f" (done: {' > '.join(steps)})" if steps else ""))
    if isinstance(res, str) and res.startswith("error"):
        code = res.split(":")[1]
        raise Fail(code, f"at {name}: {res.split(': ', 1)[1]}" + (f" (done: {' > '.join(steps)})" if steps else ""))
    steps.append(name)
    return res


@action("web_answer", level=3, group="DO", summary="answer a web question in one go: search, read the best pages, excerpts",
        params="""
        question s the question
        depth s? quick|thorough
        """, cost=8.0, tier="web", star=True, top="question", fallback="research(question)",
        modes=frozenset({"single", "director", "local"}), timeout=90, limits="returns excerpts with sources; you write the answer")
async def web_answer(ctx: Ctx, question: str, depth: str = "quick", **_) -> str:
    h = _h()
    try:
        _need_browser(ctx)
        rows = await _google(ctx, question)
    except Fail:
        if ctx.research is not None:  # no tab, or Google wants a check: the hidden researcher still can
            notes = await ctx.research(question)
            if notes and not str(notes).startswith("error"):
                return ok(f"{str(notes)[:1500]}", via="research")
        raise
    rows = [r for r in rows if not SKIP_RESULTS.search(r[1])]
    if not rows:
        if ctx.research is not None:
            notes = await ctx.research(question)
            if notes and not str(notes).startswith("error"):
                return ok(f"{str(notes)[:1500]}", via="research")
        raise Fail("NOT_FOUND", f"no usable results for {question!r}", f'research("{question[:60]}")')
    out = ["Search snippets:\n" + "\n".join(f"- {t[:80]}: {s[:200]}" for t, _u, s in rows[:3])]
    sources = []
    for title, url, _s in rows[:3 if depth == "thorough" else 2]:
        try:
            res = await _navigate(ctx, url)
            if res.startswith("error"):
                continue
            text = await beval(ctx, "() => document.body ? document.body.innerText : ''", 20)
        except Exception:
            continue
        part = h.find_in_text(text, question, budget=700)
        if part.startswith("No lines mention"):
            continue
        out.append(f"From {title[:60]}:\n{part}")
        sources.append(url)
        if depth != "thorough":
            break
    body = "\n\n".join(out)
    return ok(f"{body[:1800]}\n| sources: {', '.join(sources) or ', '.join(r[1] for r in rows[:2])} | steps: web_search > read_page")


@action("write_in_app", level=3, group="DO", summary="open an app, start a new document and type text in it (checked)",
        params="""
        app s the app, e.g. Notepad
        text s what to write
        new b? new document first (true)
        save_to s? full path, only if asked to save
        """, cost=4.0, tier="uia", star=True, top="app,text,new?", fallback="App(launch) then type_text(text)", timeout=60,
        limits="new=true keeps text out of Win11 Notepad's restored tabs; saves only with save_to")
async def write_in_app(ctx: Ctx, app: str, text: str, new: bool = True, save_to: str = "", **_) -> str:
    steps: list = []
    opened = await _step(steps, "open_app", open_app(ctx, app))
    w = fg()
    match = _app_match(app)
    if w is None or not match(w):
        w = resolve(ctx, app)
    if w is None:
        raise Fail("NOT_FOUND", f"at open_app: no {app} window after opening it", "list_windows()")
    await asyncio.sleep(0.4)
    if await clear_tips(w):  # a "What's new" popup would swallow Ctrl+N and the typing
        steps.append("closed a tip popup")
    fresh = "(new window)" in opened and re.search(r"untitled|new", _text(w.hwnd), re.I)  # the title settles after start
    if new and not fresh:
        title_before = w.title
        tsig = await tree_sig(w.hwnd)
        if not await focus(w):
            raise Fail("NOT_FOCUSED", f"at new document: couldn't bring '{w.title[:40]}' to the front (done: open_app)", "dismiss_dialog()")
        press(w, "ctrl+n")
        t_end = time.time() + 2.0
        while time.time() < t_end:
            await asyncio.sleep(0.2)
            cur = fg()
            if cur and cur.hwnd != w.hwnd and match(cur):  # some apps open a new window for Ctrl+N
                w = cur
                ctx.opened.add(w.hwnd)
                break
            t = _text(w.hwnd)
            if (t != title_before and re.search(r"untitled", t, re.I)) or (await tree_sig(w.hwnd)) != tsig:
                break
        w = _w(w.hwnd) or w
        steps.append("new document")
    if new:
        # typing replaces the editor's text only once it is certainly a new, empty document: an app where Ctrl+N did
        # nothing would otherwise lose the user's own text
        try:
            body = await on_uia(_doc_text_sync, w.hwnd, timeout=4.0)
        except UiaTimeout:
            body = None
        if body is None or _ws(body):
            raise Fail("NO_CHANGE", f"at new document: '{_text(w.hwnd)[:40]}' still shows text after Ctrl+N; nothing was typed "
                       f"(done: {' > '.join(steps)})", f'write_in_app("{app}", text, new=false) to add to it, or select_menu("File > New")')
    await _step(steps, "type_into", type_into(ctx, field="document", text=text, window=f"hwnd:{w.hwnd}", clear=False))
    first = _ws(text)[:30]
    check = await read_window(ctx, window=f"hwnd:{w.hwnd}", find=first, max=1500)
    if first and first.lower() not in _ws(check).lower():
        return unsure(f"typed into '{_text(w.hwnd)[:40]}' but reading it back doesn't show {first!r}", f'read_window("{_text(w.hwnd)[:30]}")')
    steps.append("read back")
    if save_to:
        await _step(steps, "save_file_as", save_file_as(ctx, path=save_to, window=f"hwnd:{w.hwnd}"))
    return ok(f"wrote {len(text)} characters in '{_text(w.hwnd)[:50]}'" + (f" and saved {save_to}" if save_to else "") + f" | steps: {' > '.join(steps)}")


def _doc_text_sync(hwnd: int) -> str | None:
    """The text of the window's main editor (its largest visible Document/Edit), or None when it has none."""
    nodes, elems = walk(hwnd, 3000)
    wrect = _rect(hwnd)
    docs = [x for x in nodes if x.kind in ("Document", "Edit") and x.visible_in(wrect)]
    n = max(docs, key=lambda x: (x.rect[2] - x.rect[0]) * (x.rect[3] - x.rect[1]), default=None)
    return None if n is None else _text_of_elem(elems[n.i], n)


CALC_AIDS = {**{d: f"num{d}Button" for d in "0123456789"}, "+": "plusButton", "-": "minusButton", "*": "multiplyButton",
             "/": "divideButton", ".": "decimalSeparatorButton", "%": "percentButton", "(": "openParenthesisButton",
             ")": "closeParenthesisButton", "=": "equalButton"}
CALC_WORDS = [(r"\b(times|multiplied by|x)\b|×|∗", "*"), (r"\b(divided by|over)\b|÷", "/"), (r"\bplus\b", "+"),
              (r"\b(minus|take away)\b|−", "-"), (r"\bpercent\b", "%")]


def _calc_keys(expression: str) -> str:
    """'1,234 times 5678' -> '1234*5678=': the Calculator keys to press, or Fail."""
    e = (expression or "").lower().strip().rstrip("=?. ")
    e = re.sub(r"^(what is|what's|calculate|compute)\s+", "", e)
    for pat, op in CALC_WORDS:
        e = re.sub(pat, op, e)
    e = re.sub(r"(?<=\d),(?=\d{3}\b)", "", e).replace(" ", "")
    if not e or not re.fullmatch(r"[0-9.+\-*/%()]+", e):
        raise Fail("BAD_ARGS", f"{expression!r} isn't something the Calculator's keys can type", 'calculator("1234*5678") or calc(expr)')
    return e + "="


def _calc_press_sync(hwnd: int, keys: str) -> dict:
    """COM thread: Clear, then each key by its AutomationId through Invoke (no pointer, no keyboard focus needed)."""
    nodes, elems = walk(hwnd, 1500)
    by_aid = {n.aid: n for n in nodes if n.aid and n.kind == "Button"}
    missing = sorted({k for k in keys if CALC_AIDS[k] not in by_aid})
    if missing:
        return {"missing": missing}
    for aid in ("clearButton",):
        if aid in by_aid and (p := pattern(elems[by_aid[aid].i], "invoke")):
            p.Invoke(waitTime=0)
            time.sleep(0.05)
    for k in keys:
        p = pattern(elems[by_aid[CALC_AIDS[k]].i], "invoke")
        if not p:
            return {"missing": [k]}
        p.Invoke(waitTime=0)
        time.sleep(0.04)
    return {"pressed": len(keys)}


def _calc_display_sync(hwnd: int) -> str:
    nodes, _ = walk(hwnd, 1500)
    hit = next((n for n in nodes if n.aid == "CalculatorResults"), None) or next((n for n in nodes if n.name.startswith("Display is")), None)
    return hit.name if hit else ""


@action("calculator", level=3, group="DO", summary="work out an expression in the Calculator app and read its display",
        params="expression s like 1234*5678 or 12.5 / 4", cost=3.0, tier="uia", star=True, top="expression",
        fallback='open_app("Calculator") then click("1") ... click("=")', timeout=40,
        limits="only when the user wants the Calculator app (calc answers without it); standard mode works left to right")
async def calculator(ctx: Ctx, expression: str, **_) -> str:
    keys = _calc_keys(expression)
    steps: list = []
    await _step(steps, "open_app", open_app(ctx, "Calculator"))
    w = next((x for x in windows() if x.exe in APP_EXES["calculator"] or x.title == "Calculator"), None)
    if w is None:
        raise Fail("NOT_FOUND", "at open_app: no Calculator window", "list_windows()")
    guard_input(ctx, w)
    await asyncio.sleep(0.3)
    res = await on_uia(_calc_press_sync, w.hwnd, keys, timeout=8.0)
    if res.get("missing"):
        raise Fail("UNSUPPORTED", f"at keys: Calculator shows no {' '.join(res['missing'])} key in its current mode (done: open_app)",
                   'click("Open Navigation") then click("Scientific"), or calc(expr)')
    steps.append(f"pressed {keys}")
    await asyncio.sleep(0.3)
    shown = re.sub(r"^Display is\s*", "", await on_uia(_calc_display_sync, w.hwnd, timeout=4.0)).strip()
    if not shown:
        return unsure(f"pressed {keys} in Calculator but couldn't read its display", 'read_window("Calculator", find="Display")')
    steps.append("read display")
    note = ""
    try:
        want = calculate(keys[:-1]).split(" = ", 1)[1].split(" (")[0]
        num = lambda s: float(re.sub(r"[^\d.\-]", "", s))
        if "%" not in keys and abs(num(want) - num(shown)) > 1e-6 * max(1.0, abs(num(want))):
            note = f" (calc says {want}: Calculator's standard mode works left to right)"
    except Exception:
        pass
    return ok(f"Calculator shows {shown} for {keys[:-1]}{note} | steps: {' > '.join(steps)}", now="'Calculator'")


def _setting_risky(args: dict, ctx: Ctx) -> str:
    """change_setting asks like set_control on the Settings window: security, privacy, accounts, updates."""
    text = f"{args.get('page', '')} {args.get('setting', '')}"
    return f"change the setting \"{args.get('setting')}\" to {args.get('value')}" if SENSITIVE_SETTING.search(text) else ""


@action("change_setting", level=3, group="DO", summary="open a Settings page and set one control on it, read back",
        params="""
        page s Settings page, e.g. display, bluetooth, colors
        setting s the control's label, e.g. Night light, Bluetooth
        value s on/off, an option's text, or a number
        """, cost=4.0, tier="uia", star=True, top="page,setting,value", fallback="open_settings(page) then set_control(label, value)",
        timeout=60, risky=lambda a, c: _setting_risky(a, c), limits="security/privacy/account settings ask the user first")
async def change_setting(ctx: Ctx, page: str, setting: str, value: str, **_) -> str:
    steps: list = []
    await _step(steps, "open_settings", open_settings(ctx, page))
    w = next((x for x in windows() if x.title == "Settings" or x.exe == "systemsettings.exe"), None)
    if w is None:
        raise Fail("NOT_FOUND", "at open_settings: no Settings window", "list_windows()")
    win = f"hwnd:{w.hwnd}"
    try:
        res = await set_control(ctx, label=setting, value=value, window=win)
    except Fail as e:
        if not e.result.startswith("error:NOT_FOUND"):
            raise Fail(e.result.split(":")[1], f"at set_control: {e.result.split(': ', 1)[1]} (done: {' > '.join(steps)})")
        # further down the page: bring it into view, then once more
        await _step(steps, "scroll_until", scroll_until(ctx, target=setting, window=win))
        res = await _step(steps, "set_control", set_control(ctx, label=setting, value=value, window=win))
    else:
        steps.append("set_control")
    return (res + f" | steps: {' > '.join(steps)}") if res.startswith(("ok:", "unsure:")) else res


@action("open_file", level=3, group="DO", summary="find a document by name in the user's folders and open it",
        params="""
        name s the file's name or part of it
        app s? open it in this app instead of its default
        """, cost=3.0, star=True, top="name,app?", fallback="find_file(name) then open_path(path)", timeout=60,
        risky=lambda a, c: _open_risky({"path": "", "app": a.get("app")}, c),
        limits="several matches: lists them (ask_user which); never runs programs or scripts")
async def open_file(ctx: Ctx, name: str, app: str = "", **_) -> str:
    steps: list = []
    found = await _step(steps, "find_file", find_file(ctx, name))
    paths = [Path(l.split("  ")[0]) for l in found.split("\n")[1:] if re.match(r"^[A-Za-z]:\\", l)]
    paths = [p for p in paths if p.exists() and p.is_file()]
    if not paths:
        raise Fail("NOT_FOUND", f"at find_file: no file like {name!r}", f'find_file("{name}", where="C:\\\\") or ask_user')
    exact = [p for p in paths if p.stem.lower() == Path(name).stem.lower() or p.name.lower() == name.lower()]
    pick = exact if len(exact) == 1 else paths if len(paths) == 1 else []
    if not pick:
        listed = "; ".join(str(p) for p in (exact or paths)[:6])
        raise Fail("AMBIGUOUS", f"{len(exact or paths)} files match {name!r}: {listed}", "ask_user which one, then open_path(path)")
    p = pick[0]
    if LAUNCHABLE.search(p.name):
        raise Fail("BLOCKED", f"{p} is a program or script, not a document", f'open_path("{p}") (asks the user first)')
    res = await _step(steps, "open_path", open_path(ctx, str(p), app=app))
    return (res + f" | steps: {' > '.join(steps)}") if res.startswith(("ok:", "unsure:")) else res


def _button_sync(hwnd: int, names: list[str], aids: tuple = ()) -> dict:
    nodes, _ = walk(hwnd, 2000)
    for n in nodes:
        if n.kind in ("Button", "SplitButton") and ((n.aid and n.aid in aids) or _norm(n.name) in names) and n.enabled is not False:
            return {"center": n.center, "name": n.name}
    return {"texts": " ".join(n.name for n in nodes if n.kind == "Text" and len(n.name) > 10)[:200]}


@action("save_file_as", level=3, group="DO", summary="save the document under a full path via the Save As dialog",
        params="""
        path s full path to save to
        window s? part of the window title
        format s? "Save as type" option, if needed
        """, cost=4.0, tier="uia", star=True, top="path", fallback='hotkeys(["ctrl+shift+s"]) then type_into("File name", path)', timeout=60,
        hide=("format",),
        limits="asks before replacing an existing file unless the request said overwrite")
async def save_file_as(ctx: Ctx, path: str, window: str = "", format: str = "", **_) -> str:
    steps: list = []
    target = _path(path)
    writable(ctx, target)
    w = _need(ctx, window)
    guard_input(ctx, w)
    if not await focus(w):
        raise Fail("NOT_FOCUSED", f"at focus: couldn't bring '{w.title[:40]}' to the front", "dismiss_dialog()")
    before = {x.hwnd for x in windows(owned=True)}
    is_dialog = lambda x: x.pid == w.pid and (x.cls == "#32770" or re.search(r"\bsave\b", x.title, re.I))
    press(w, "ctrl+shift+s")
    dlg = await _wait_window(before, is_dialog, 1.5)
    if dlg is None:
        await _step(steps, "select_menu", select_menu(ctx, "File > Save as", window=f"hwnd:{w.hwnd}"))
        dlg = await _wait_window(before, is_dialog, 3.0)
    if dlg is None:
        raise Fail("TIMEOUT", "at open dialog: no Save As dialog appeared", f'select_menu("File > Save As", window="{w.title[:30]}")')
    steps.append("Save As dialog")
    ctx.dialogs.add(dlg.hwnd)
    await _step(steps, "file name", type_into(ctx, field="File name", text=str(target), window=f"hwnd:{dlg.hwnd}"))
    if format:
        await _step(steps, "save as type", set_control(ctx, label="Save as type", value=format, window=f"hwnd:{dlg.hwnd}"))
    btn = await on_uia(_button_sync, dlg.hwnd, ["save"], ("1",), timeout=4.0)
    if "center" not in btn:
        raise Fail("NOT_FOUND", f"at Save button: no Save button in '{dlg.title[:40]}'", "hotkeys([\"enter\"])")
    before2 = {x.hwnd for x in windows(owned=True)}
    await _click_xy(ctx, dlg, btn["center"], "left", False)  # a real click: Invoke would block while a confirm dialog is open
    steps.append("Save")
    t_end = time.time() + 6
    while time.time() < t_end:
        await asyncio.sleep(0.25)
        confirm = next((x for x in _new_windows(before2) if x.pid == w.pid), None)
        if confirm is not None and _alive(dlg.hwnd):
            info = await on_uia(_button_sync, confirm.hwnd, ["yes"], (), timeout=4.0)
            texts = (await on_uia(_dismiss_sync, confirm.hwnd, [], False, timeout=4.0)).get("text", "")
            if re.search(r"exists|replace|overwrite", texts + confirm.title, re.I):
                allow = _named(ctx, "overwrite", "replace")
                if not allow:
                    answer = (await ctx.ask(f"{target} already exists. Replace it? (yes/no)")) if ctx.ask else "no"
                    allow = str(answer).strip().lower().startswith("y")
                choice = ["yes"] if allow else ["no"]
                b = await on_uia(_button_sync, confirm.hwnd, choice, ("6",) if allow else ("7",), timeout=4.0)
                if "center" in b:
                    await _click_xy(ctx, confirm, b["center"], "left", False)
                if not allow:
                    await asyncio.sleep(0.4)
                    await on_uia(_press_button_sync, dlg.hwnd, ["cancel"], timeout=4.0)
                    raise Fail("REFUSED", f"{target} exists and the user didn't want it replaced (done: {' > '.join(steps)})",
                               f'save_file_as("{target.with_stem(target.stem + "-2")}")')
                steps.append("replace")
                continue
            raise Fail("UNSUPPORTED", f"at Save: the dialog says \"{texts[:150]}\" (done: {' > '.join(steps)})", "save_file_as with another path")
        if not _alive(dlg.hwnd):
            break
    if _alive(dlg.hwnd):
        info = await on_uia(_button_sync, dlg.hwnd, ["__none__"], (), timeout=4.0)
        raise Fail("NO_CHANGE", f"at Save: the dialog is still open: {info.get('texts', '')[:150]}", f'read_window("hwnd:{dlg.hwnd}")')
    ctx.dialogs.discard(dlg.hwnd)
    await asyncio.sleep(0.3)
    if not target.exists():
        raise Fail("NO_CHANGE", f"the dialog closed but {target} isn't there (done: {' > '.join(steps)})", f'find_file("{target.name}")')
    title = _text(w.hwnd)
    note = "" if target.stem.lower() in title.lower() else f" (the window title reads '{title[:40]}')"
    return ok(f"saved {target} ({_size(target)}){note} | steps: {' > '.join(steps)}")


@action("fill_form", level=3, group="DO", summary='fill several fields: {"label": "value"}; window="web" for IO\'s tab',
        params="""
        fields o {"label": "value", ...}
        window s? part of the window title, or web
        """, cost=3.0, tier="uia", star=True, top="fields", fallback="type_into(field, text) per field", timeout=90,
        limits="never passwords, payment or ID fields; lists new fields if a wizard page follows")
async def fill_form(ctx: Ctx, fields: dict, window: str = "", **_) -> str:
    if not isinstance(fields, dict) or not fields:
        raise Fail("BAD_ARGS", 'fields is {"label": "value"}', 'fill_form({"Name": "Chris"})')
    if window.lower().strip() == "web":
        return await web_fill(ctx, fields)
    w = _need(ctx, window)
    tsig = await tree_sig(w.hwnd)
    filled, missing = [], []
    for label, value in fields.items():
        value = str(value)
        first, second = (set_control, type_into) if value.strip().lower() in ON | OFF else (type_into, set_control)
        res = ""
        for fn in (first, second):
            try:
                res = await (fn(ctx, label, value, window=f"hwnd:{w.hwnd}") if fn is set_control else
                             fn(ctx, field=label, text=value, window=f"hwnd:{w.hwnd}"))
                break
            except Fail as e:
                res = e.result
                if not re.match(r"error:(NOT_FOUND|BAD_ARGS)", res):
                    break
        (filled if res.startswith("ok") else missing).append(label if res.startswith("ok") else f"{label} ({res[6:70]})")
    more = ""
    if _alive(w.hwnd) and (await tree_sig(w.hwnd)) != tsig and filled:
        found = await on_uia(_controls_sync, w.hwnd, {"Edit", "ComboBox", "CheckBox", "RadioButton"}, "", timeout=4.0)
        names = [nm for _k3, nm, *_r in found if nm and nm not in fields][:10]
        more = f"; fields on screen now: {', '.join(names)}" if names else ""
    if not filled:
        raise Fail("NOT_FOUND", f"no fields filled: {'; '.join(missing)}", f'list_controls("{w.title[:30]}", kind="field")')
    text = f"filled: {', '.join(filled)}" + (f"; not filled: {'; '.join(missing)}" if missing else "") + more
    return unsure(text, f'list_controls("{w.title[:30]}", kind="field")') if missing else ok(text)


@action("cleanup", level=3, group="DO", summary="put away IO's own leftovers (its tab, abandoned dialogs)", params="", cost=1.0,
        modes=frozenset({"internal"}), timeout=8, limits="internal: runs when a task ends; never closes what the user asked for")
async def cleanup(ctx: Ctx, **_) -> str:
    done = []
    if ctx.tab_open and ctx.browser is not None:
        res = await web_close_tab(ctx)
        done.append("tab" if res.startswith("ok") else "tab?")
    for hwnd in list(ctx.dialogs):
        if _alive(hwnd):
            _u.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE on a dialog is its Cancel
            done.append(f"dialog '{_text(hwnd)[:30]}'")
        ctx.dialogs.discard(hwnd)
    try:
        _h().hint_focus("")
    except Exception:
        pass
    return ok("cleaned up: " + (", ".join(done) if done else "nothing to put away"))


# ======================================================================================================================
# L3 · GAME: loop helpers (they exist to cut vision calls)
# ======================================================================================================================

GAME_MODES = frozenset({"loop", "director", "local"})
GAME_PROMPT = ("This is a screenshot of a game. Answer only with JSON: {\"screen\": \"which screen or menu is open, short\", "
               "\"can_do\": [\"up to 4 things that can be done next, by their button names\"], \"popup\": true or false, "
               "\"hud\": {\"<resource or counter name>\": \"<value as shown>\"}}")


def _need_loop(ctx: Ctx) -> W:
    if not (ctx.loop and ctx.focus):
        raise Fail("NEEDS", "game actions work in a loop locked on a window", 'click(target, how="vision") or click_on(description)')
    w = resolve(ctx, ctx.focus)
    if w is None:
        raise Fail("NOT_FOUND", f"the {ctx.focus} window isn't open", "list_windows()")
    return w


def _content(ctx: Ctx, w: W) -> tuple:
    return _h().content_rect(ctx.focus) or _rect(w.hwnd)


def _num(v) -> float | None:
    """'1.2K' -> 1200, '3,456' -> 3456, '12/50' -> 12; None if it isn't a number."""
    m = re.search(r"(-?\d+(?:[.,]\d+)*)\s*([kmbt])?\b", str(v).replace(" ", ""), re.I)
    if not m:
        return None
    s = m.group(1)
    s = s.replace(",", "") if re.search(r",\d{3}\b", s) or "." in s else s.replace(",", ".")
    try:
        x = float(s)
    except ValueError:
        return None
    return x * {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}.get((m.group(2) or "").lower(), 1)


def _trend(hud: list) -> tuple[str, bool]:
    """Per-minute change of each numeric HUD value over the last 10 minutes, and whether nothing rose in the last 5 reads."""
    if len(hud) < 2:
        return "", False
    now_t, now = hud[-1]
    bits = []
    for k, v in now.items():
        old = next(((t, h[k]) for t, h in hud if k in h and now_t - t <= 600), None)
        if old and now_t - old[0] >= 20:
            rate = (v - old[1]) / ((now_t - old[0]) / 60)
            bits.append(f"{k} {'+' if rate >= 0 else ''}{rate:.3g}/min")
    last = hud[-5:]
    stalled = len(last) == 5 and not any(any(h2.get(k, 0) > h1.get(k, 0) for k in h2) for (_a, h1), (_b, h2) in zip(last, last[1:]))
    return ", ".join(bits), stalled


@action("game_state", level=3, group="GAME", summary="the game's screen, what to do next, popups, HUD numbers and trends",
        params="", cost=3.0, tier="vision", modes=GAME_MODES, top="", fallback="look_at_screen(question)", timeout=40,
        limits="cached (instant) until the screen changes; numbers come from vision and can be misread")
async def game_state(ctx: Ctx, **_) -> str:
    w = _need_loop(ctx)
    rect = _content(ctx, w)
    await focus(w)  # before the signature: activating a window repaints its frame
    sig = await sig_of(rect)
    cache = ctx.game_cache
    if cache.get("sig") is not None and sig is not None and not changed(cache["sig"], sig) and time.time() - cache.get("t", 0) < 180:
        return ok(cache["text"] + " [cached: the screen hasn't changed]")
    img = await asyncio.to_thread(grab, rect)
    reply = await asyncio.to_thread(vlm, img, GAME_PROMPT, 220)
    m = re.search(r"\{.*\}", reply, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    if not data:
        return unsure(f"couldn't read the game's state: {reply[:150]}", "look_at_screen(\"what is on screen?\")")
    hud = data.get("hud") if isinstance(data.get("hud"), dict) else {}
    nums = {str(k)[:30]: n for k, v in hud.items() if (n := _num(v)) is not None}
    ctx.hud[:] = (ctx.hud + [(time.time(), nums)])[-60:]
    trend, stalled = _trend(ctx.hud)
    can = data.get("can_do") if isinstance(data.get("can_do"), list) else []
    text = (f"screen: {str(data.get('screen', '?'))[:120]}; can do: {', '.join(str(c)[:40] for c in can[:4]) or '?'}; "
            f"popup: {'yes' if data.get('popup') else 'no'}; hud: " + (", ".join(f"{k}={v}" for k, v in list(hud.items())[:8]) or "none") +
            (f"; trend: {trend}" if trend else "") + ("; stalled: nothing rose over the last 5 reads" if stalled else ""))
    cache.update(sig=sig, t=time.time(), text=text)
    return ok(text, via="vision")


@action("tap_repeatedly", level=3, group="GAME", summary="find something once, then tap it many times; stops if a popup appears",
        params="""
        target s what to tap (a look), or "x,y"
        times i? how many taps (10, at most 50)
        interval n? seconds between taps (0.3)
        refind_every i? look again every N taps (0 = never)
        """, cost=4.0, tier="vision", modes=GAME_MODES, top="target,times?", fallback="click_on(description)", timeout=120,
        limits="one vision call; a big screen change (popup, new menu) stops it so nothing is tapped under an ad")
async def tap_repeatedly(ctx: Ctx, target: str, times: int = 10, interval: float = 0.3, refind_every: int = 0, **_) -> str:
    w = _need_loop(ctx)
    m = XY.match(target)
    pt = (int(m.group(1)), int(m.group(2))) if m else (await vision_point(ctx, w, target))[0]
    loop_guard(ctx, pt)
    await _uncovered(w, pt)
    if not m:
        note_point(ctx, pt)
    rect = _content(ctx, w)
    times = builtins_max(1, min(int(times or 10), 50))
    interval = builtins_max(0.05, min(float(interval or 0.3), 5.0))
    q = _h().quick_click
    await asyncio.to_thread(q, *pt)
    await asyncio.sleep(interval)
    a = await sig_of(rect)
    await asyncio.sleep(0.15)
    b = await sig_of(rect)
    noise = sig_diff(a, b)
    last = b
    taps = 1
    for k in range(1, times):
        if refind_every and k % refind_every == 0 and not m:
            pt = (await vision_point(ctx, w, target))[0]
            loop_guard(ctx, pt)
        await asyncio.to_thread(q, *pt)
        taps += 1
        await asyncio.sleep(interval)
        if taps % 3 == 0:
            now = await sig_of(rect)
            # counters and sparkles change a little all the time; a popup or a new menu changes a big part of the window
            mean, frac = sig_diff(last, now)
            if frac > builtins_max(0.08, 3 * noise[1]) and mean > builtins_max(3.0, 3 * noise[0]):
                return ok(f"stopped after {taps} taps at ({pt[0]}, {pt[1]}): the screen changed a lot (popup or new menu?)",
                          now="game_state() or close_popups()")
            last = now
    return ok(f"tapped ({pt[0]}, {pt[1]}) {taps} times, {interval:g}s apart")


def _press_at(x: int, y: int) -> tuple[int, int]:
    home = _cursor()
    _u.SetCursorPos(int(x), int(y))
    time.sleep(0.04)
    _u.mouse_event(0x0002, 0, 0, 0, 0)
    return home


def _release(home: tuple[int, int]) -> None:
    _u.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.02)
    _u.SetCursorPos(*home)


@action("hold_until", level=3, group="GAME", summary="press and hold on something until the screen changes (or settles)",
        params="""
        target s what to hold on (a look), or "x,y"
        max_seconds n? longest hold (15)
        until s? change|still
        """, cost=5.0, tier="vision", modes=GAME_MODES, top="target", fallback="hold_on(description, seconds)", timeout=60,
        limits="the button is always released, even when stopped")
async def hold_until(ctx: Ctx, target: str, max_seconds: float = 15, until: str = "change", **_) -> str:
    w = _need_loop(ctx)
    m = XY.match(target)
    pt = (int(m.group(1)), int(m.group(2))) if m else (await vision_point(ctx, w, target))[0]
    loop_guard(ctx, pt)
    await _uncovered(w, pt)
    rect = _content(ctx, w)
    a = await sig_of(rect)
    await asyncio.sleep(0.15)
    b = await sig_of(rect)
    noise = sig_diff(a, b)
    limit = builtins_max(0.3, min(float(max_seconds or 15), 30))
    # pressed on this thread (~40 ms), not in a worker: a Stop landing while a worker pressed would skip the finally below
    # and leave the left button held down
    home = _press_at(*pt)
    t0, why = time.time(), "time limit"
    try:
        start, prev, still_since = b, b, time.time()
        while time.time() - t0 < limit:
            await asyncio.sleep(0.2)
            now = await sig_of(rect)
            if until == "change" and changed(start, now, noise):
                why = "the screen changed"
                break
            if until == "still":
                if changed(prev, now, noise):
                    still_since = time.time()
                elif time.time() - still_since >= 0.6 and time.time() - t0 > 0.8:
                    why = "the screen settled"
                    break
            prev = now
    finally:
        _release(home)  # even if the task is stopped mid-hold (synchronous: a second Stop can't cut it short)
    return ok(f"held ({pt[0]}, {pt[1]}) for {time.time() - t0:.1f}s until {why}")


def _drag(x0: int, y0: int, x1: int, y1: int, ms: int = 300) -> None:
    home = _press_at(x0, y0)
    try:
        steps = 15
        for i in range(1, steps + 1):
            time.sleep(ms / 1000 / steps)
            _u.SetCursorPos(int(x0 + (x1 - x0) * i / steps), int(y0 + (y1 - y0) * i / steps))
        time.sleep(0.05)
    finally:
        _release(home)


async def game_swipe(ctx: Ctx, w: W, direction: str, distance: float = 0.4) -> str:
    """A finger swipe across the content area: 'up' moves the finger up (the page scrolls down)."""
    l, t, r, b = _content(ctx, w) if ctx.focus else _rect(w.hwnd)
    cx, cy = (l + r) // 2, (t + b) // 2
    d = builtins_max(0.1, min(float(distance or 0.4), 0.9))
    dx = {"left": -1, "right": 1}.get(direction, 0) * d * (r - l) / 2
    dy = {"up": -1, "down": 1}.get(direction, 0) * d * (b - t) / 2
    await asyncio.to_thread(_drag, int(cx - dx), int(cy - dy), int(cx + dx), int(cy + dy))
    return f"swiped {direction} from ({int(cx - dx)}, {int(cy - dy)}) to ({int(cx + dx)}, {int(cy + dy)})"


@action("swipe", level=3, group="GAME", summary="swipe the finger up/down/left/right across the game (emulator scroll)",
        params="""
        direction s up|down|left|right
        distance n? share of the window (0.4)
        """, cost=0.5, tier="exact", modes=GAME_MODES, top="direction", fallback="Scroll(loc, direction)")
async def swipe(ctx: Ctx, direction: str, distance: float = 0.4, **_) -> str:
    w = _need_loop(ctx)
    await focus(w)
    return ok(await game_swipe(ctx, w, direction, distance))


@action("close_popups", level=3, group="GAME", summary="close popups by their X/Close button (never ads or purchases)",
        params="max i? most popups to close (3)", cost=6.0, tier="vision", modes=GAME_MODES, top="", fallback="click_on(\"the X button\")",
        timeout=90, limits="asks vision first whether a popup is there, so nothing is clicked on a clear screen")
async def close_popups(ctx: Ctx, max: int = 3, **_) -> str:
    w = _need_loop(ctx)
    rect = _content(ctx, w)
    closed = 0
    for _i in range(builtins_max(1, min(int(max or 3), 6))):
        ans, why = await yes_no(ctx, "Is a popup, dialog or overlay with a close (X) button covering part of the game?", w)
        if ans != "yes":
            break
        pt, _rung = await vision_point(ctx, w, "the X or Close button of the popup or dialog (not an ad, not a purchase, not a play button)")
        loop_guard(ctx, pt)
        note_point(ctx, pt)
        before = await sig_of(rect)
        await asyncio.to_thread(_h().quick_click, *pt)
        await asyncio.sleep(0.6)
        if not changed(before, await sig_of(rect)):
            return unsure(f"clicked ({pt[0]}, {pt[1]}) on a popup's X but nothing changed (closed {closed} before)", "game_state()")
        closed += 1
    return ok(f"closed {closed} popup{'s' if closed != 1 else ''}" if closed else "no popup to close")


# ======================================================================================================================
# meta: tools(group) and use(name, args)
# ======================================================================================================================

@action("tools", group="END", summary="list the actions in a group with their costs and limits",
        params="group s WIN, READ, ACT, SEE, FILE, PC, WEB, DO, GAME or RAW", cost=0.0, top="group", fallback="")
async def tools_(ctx: Ctx, group: str, **_) -> str:
    g = group.strip().upper()
    if g not in GROUPS:
        return err("BAD_ARGS", f"no group {group!r}", "tools(\"" + "|".join(x for x in GROUPS if x != "END") + "\")")
    return ok(catalog_group(g, [n for n in available(ctx) if ctx.allowed is None or ctx.allowed(n)]))


@action("use", group="END", summary="run any action by name with its args (tools(group) lists them)",
        params="""
        name s the action's name
        args o its arguments
        """, cost=0.0, modes=frozenset({"single", "loop", "local"}), top="name,args", fallback="")
async def use(ctx: Ctx, name: str, args: dict | None = None, **_) -> str:
    """boss unwraps use() before its checks (constraints, confirmation, toggles), so a call that still arrives here was
    not checked: anything but a plain, allowed, harmless action is refused rather than run unchecked."""
    a = REGISTRY.get(name)
    if a is None or name in ("use", "tools"):
        close = difflib.get_close_matches(name, [n for n in REGISTRY if n != "use"], n=3)
        return err("BAD_ARGS", f"no action named {name!r}", " or ".join(close) or 'tools("ACT")')
    if a.fn is None:
        return err("UNSUPPORTED", f"call {name} directly; it isn't run through use()", a.signature())
    args = args if isinstance(args, dict) else {}
    if (ctx.allowed is not None and not ctx.allowed(name)) or available(ctx).count(name) == 0:
        return err("BLOCKED", f"{name} isn't available in this task", 'tools("ACT")')
    if constraint_block(name, args, ctx) or risky(name, args, ctx):
        return err("BLOCKED", f"call {name} directly, not through use(), so the user can be asked", a.signature())
    return await call(name, args, ctx)


# ======================================================================================================================
# L1: existing primitives (boss executes them; registered so catalogs, menus and the planner come from one place)
# ======================================================================================================================

SINGLE = frozenset({"single", "director", "local"})
external("App", group="RAW", summary="launch, switch to or resize an app by name (prefer open_app)",
         params="""
         mode s launch|switch|resize
         name s the app's name
         """, cost=2.0, limits="no check that its window came up")
external("Snapshot", group="RAW", summary="all windows and controls with (x,y), ~1s (prefer read_window)",
         params="", cost=1.0, limits="capped at 500 elements")
external("Click", group="RAW", summary="click at loc=[x, y] from list_controls or find_on_screen",
         params="""
         loc a [x, y] screen coordinates
         button s? left|right|middle
         clicks i? 1 or 2
         """, cost=0.3, limits="never guessed coordinates")
external("Type", group="RAW", summary="click loc=[x, y] then type text there",
         params="""
         loc a [x, y] screen coordinates
         text s what to type
         clear b? replace what's there
         press_enter b? press Enter afterwards
         """, cost=0.5)
external("Scroll", group="RAW", summary="mouse-wheel scroll at loc=[x, y]",
         params="""
         loc a? [x, y] where to scroll
         direction s? up|down|left|right
         wheel_times i? how many notches
         """, cost=0.3)
external("Shortcut", group="RAW", summary="press a key combination like ctrl+s, alt+tab, enter",
         params="shortcut s keys like ctrl+s", cost=0.2, limits="no Esc/Back in games")
external("WaitFor", group="RAW", summary="Windows-MCP wait for a condition (prefer wait_until)",
         params="""
         condition s what to wait for
         text s? text to wait for
         window_name s? window title
         timeout n? seconds
         """, cost=2.0, modes=SINGLE)
external("PowerShell", group="RAW", summary="run a PowerShell command, get its output (when no action fits)",
         desc=("Time in a city: [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'Tokyo Standard Time')."
               "ToString('h:mm tt'). Installed apps: Get-ItemProperty HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*, "
               "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\* | Where-Object DisplayName | Select-Object DisplayName"),
         params="command s the PowerShell command", cost=1.0, limits="deleting or killing asks the user first")
external("Clipboard", group="RAW", summary="get or set the clipboard (only when the user asks about the clipboard)",
         params="""
         mode s get|set
         text s? text to set
         """, cost=0.1, modes=SINGLE)
external("Process", group="RAW", summary="list or kill processes", params="""
         mode s list|kill
         name s? process name
         """, cost=0.5, modes=SINGLE, limits="kill asks the user first")
external("FileSystem", group="RAW", summary="Windows-MCP file read/write/list (prefer the FILE actions)", params="""
         mode s read|write|copy|move|delete|list|search|info
         path s the path
         content s? text to write
         """, cost=0.3, modes=SINGLE)
external("Scrape", group="RAW", summary="fetch a web page as text without the browser (some sites block it)",
         params="url s the address", cost=2.0, modes=SINGLE)
external("type_text", group="ACT", summary="type text where the keyboard is (an editor that just opened)",
         params="""
         text s what to type
         press_enter b? press Enter afterwards
         """, cost=0.3, limits="use type_into for a labelled field")
external("close_windows", group="WIN", summary="close windows by title like their X (prefer close_window)",
         params="""
         titles a? parts of window titles
         all_except a? close all others
         """, cost=1.0, modes=SINGLE)
external("look_at_screen", group="SEE", summary="describe what a window or display shows (vision)",
         params="""
         question s what you want to know
         window s? part of the window title
         """, cost=5.0, tier="vision", star=False, limits="can't see IO's own browser tab")
external("find_on_screen", group="SEE", summary="where something is on screen, as x,y (vision)",
         params="""
         description s what to find
         window s? part of the window title
         """, cost=3.0, tier="vision", limits="the eyes can point at a near miss; check the result")
external("click_on", group="SEE", summary="find something by how it looks and click it (vision)",
         params="""
         description s what to click
         window s? part of the window title
         """, cost=3.5, tier="vision", limits="can miss; prefer click(target) in normal apps")
external("hold_on", group="GAME", summary="find something by how it looks and hold the mouse on it",
         params="""
         description s what to hold on
         seconds n? how long (0.2-15)
         window s? part of the window title
         """, cost=4.0, tier="vision")
external("hold", group="GAME", summary="hold the mouse at loc=[x, y] for some seconds",
         params="""
         loc a [x, y] screen coordinates
         seconds n? how long (0.2-15)
         """, cost=2.0)
external("wait", group="GAME", summary="pause for some seconds (game timers; for UI use wait_until)",
         params="""
         seconds n how long (0.5-600)
         reason s? what you wait for
         """, cost=1.0)
for _bname, _bsum, _bparams in (
        ("browser_open", "open a web page in IO's tab (prefer read_page)", "url s? the address"),
        ("browser_read", "read the page in IO's tab; find= for facts (prefer read_page)", "find s? words to look for"),
        ("browser_snapshot", "the page's elements with refs, for browser_click/browser_type", ""),
        ("browser_click", "click a page element by its ref from browser_snapshot", "element s? its description\ntarget s the ref, like e12"),
        ("browser_type", "type into a page field by its ref from browser_snapshot",
         "element s? its description\ntarget s the ref, like e12\ntext s what to type\nsubmit b? press Enter"),
        ("browser_press_key", "press a key in IO's tab", "key s like Enter or ArrowDown"),
        ("browser_navigate_back", "go back a page in IO's tab", ""),
        ("browser_select_option", "choose a dropdown option by ref", "element s? its description\ntarget s the ref\nvalues a the option texts")):
    external(_bname, group="RAW", summary=_bsum, params=_bparams, cost=2.0, tier="web", modes=SINGLE, limits="IO's own tab only")
external("ask_user", group="END", summary="ask the user a question and wait for the answer",
         params="question s the question", cost=10.0, star=True, top="question", modes=SINGLE)
external("remember", group="END", summary="save a lasting note for future tasks (a path, a preference)",
         params="note s the note", cost=0.0, star=True, top="note")
external("research", group="END", summary="look something up on the web without leaving the app (hidden browser)",
         params="question s what to find out", cost=20.0, tier="web", star=True, top="question")
external("ask_gemini", group="END", summary="ask a stronger model when stuck after research",
         params="question s what you're stuck on", cost=30.0, tier="llm", modes=frozenset({"loop", "local"}))
external("todo", group="END", summary="write or update your todo list for a task with several parts (the user watches it as a checklist)",
         params="""
         items a every part in order: {"text": ..., "status": "done|in_progress|todo"}
         """, cost=0.0, top="items", modes=frozenset({"single", "loop", "local", "director"}))
REGISTRY["todo"].params["items"]["items"] = {
    "type": "object", "properties": {"text": {"type": "string"}, "status": {"type": "string", "enum": ["done", "in_progress", "todo"]}},
    "required": ["text", "status"]}
external("add_goal", group="END", summary="make an ongoing ask a standing goal IO checks on by itself every N minutes, in its own chat",
         params="""
         objective s the goal in full: what to watch or keep doing, and when it counts as done
         every_minutes i? how often to check in (30)
         title s? a short name
         """, cost=0.0, top="objective", modes=frozenset({"single", "local", "director"}))
external("notes", group="END", summary="read the notes you saved from earlier runs of a task, by the name listed with the request",
         params="name s the notes' name", cost=0.0, top="name", modes=frozenset({"single", "loop", "local", "director"}))
external("ask_model", group="END", summary="ask another AI model a question; pick it by what it's good at (the model list says)",
         params="""
         model s which model, by its name in the list
         question s what you want to know, specifically
         look b? also show it the window you're working in
         """, cost=10.0, tier="llm", modes=frozenset({"single", "loop", "local", "director"}), top="model,question")
external("done", group="END", summary="finish: summary = the answer for the user (in a loop: a progress note)",
         params="summary s the answer, or what blocked it", cost=0.0, star=True, top="summary")
external("steps", group="END", summary="do several actions in one turn, in order; stops early if one fails or the screen changes a lot",
         params="""
         steps a actions in order (tool + args), at most 8
         """, cost=0.0, star=True, top="steps", modes=frozenset({"single", "loop", "local", "director"}),
         limits="no done, ask_user or steps inside; each step gets its own result")
REGISTRY["steps"].params["steps"]["items"] = {
    "type": "object", "properties": {"tool": {"type": "string"}, "args": {"type": "object"}}, "required": ["tool"]}
STEPS_MAX = 8
STEPS_BANNED = {"steps", "done", "ask_user", "tools"}


def expand_steps(raw) -> tuple[list[tuple[str, dict]], str]:
    """steps(...)'s list -> ([(tool, args)], note), or ([], the problem). Each step may be {"tool", "args"},
    {"name", "arguments"}, or the text form small models write: 'click_on({"description": "the ore"})'."""
    if isinstance(raw, str):
        raw = _loose(raw)
    if not isinstance(raw, list) or not raw:
        return [], 'steps needs a list like [{"tool": "click_on", "args": {"description": "..."}}]'
    out = []
    for s in raw[:STEPS_MAX]:
        if isinstance(s, str):
            m = re.match(r"\s*([A-Za-z_]\w*)\s*\((.*)\)\s*$", s, re.S)
            s = {"tool": m.group(1), "args": _loose(m.group(2)) or {}} if m else {"tool": s.strip()}
        if not isinstance(s, dict):
            continue
        tool = str(s.get("tool") or s.get("name") or s.get("action") or "").strip()
        args = s.get("args", s.get("arguments", {k: v for k, v in s.items() if k not in ("tool", "name", "action")}))
        if isinstance(args, str):
            args = _loose(args) or {}
        if tool:
            out.append((tool, args if isinstance(args, dict) else {}))
    banned = [t for t, _ in out if t in STEPS_BANNED]
    if banned:
        return [], f"{', '.join(dict.fromkeys(banned))} can't go inside steps; call it on its own"
    if not out:
        return [], 'no usable steps; each needs a tool name, like {"tool": "click_on", "args": {"description": "..."}}'
    extra = f" (only the first {STEPS_MAX} of {len(raw)} were run)" if len(raw) > STEPS_MAX else ""
    return out, extra


def _loose(text: str):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


# ======================================================================================================================
# routing: regex rules on the resolved request, no LLM call
# ======================================================================================================================

@dataclass
class Route:
    kind: str
    menu: list                  # the local model's preloaded tools, in preference order (14 at most outside loops)
    expand: list                # groups preloaded fully expanded for the director
    decider: str                # "local" or "director" (when the director is on)
    director_after: int | None  # failures before handing over to the director; None = never; 0 = director decides
    planner: str                # "no" | "if_director_off" | "as_today"


SMALL_TALK = re.compile(r"^(hi|hello|hey|yo|hiya|howdy|sup|thanks|thank you|thx|ty|cheers|good (morning|afternoon|evening|night)|"
                        r"how are you|how's it going|what's up|who are you|what are you|what can you do|nice|cool|great|ok|okay)\b", re.I)
APP_WORDS = (r"notepad|calculator|calc|file explorer|explorer|paint|word|excel|powerpoint|outlook|bluestacks|terminal|task manager|vs ?code|"
             r"spotify|steam|photos|snipping tool|clock|control panel|wordpad|settings app")
R_ABOUT = re.compile(r"who are you|what can you do|what is io\b|about yourself", re.I)
R_PATH = re.compile(r"[a-z]:\\|%\w+%|~[\\/]|\\\\", re.I)
R_FILEWORDS = re.compile(r"\b(file|files|folder|folders|rename|zip|unzip|newest|largest|recycle|move|copy)\b", re.I)
R_IN_APP = re.compile(r"\b(open|click|type|write)\b.*\bin\b", re.I)
# "move this window to my left monitor", "copy the text from notepad into a new file": windows and app text, not files
R_WINDOW_THING = re.compile(r"\b(this|the|that|a|my)\s+(window|tab)\b|\b(monitor|screen|display)\b|"
                            r"\b(text|words|contents?|lines?)\b.{0,30}\b(from|in|of)\b.{0,12}\b(" + APP_WORDS + r")\b", re.I)
# a request that doesn't say which one: the user has to be asked (never guessed from Recent items)
R_VAGUE = re.compile(r"\b(the|that|my)\s+(document|file|doc|project|thing|spreadsheet|presentation|one|page|tab)\s+(?:that\s+)?"
                     r"(i\s+was|i'?m|i\s+am|i\s+had|i\s+have\s+been|we\s+were)\s+(working on|editing|using|looking at|reading|writing|on)\b|"
                     r"\b(the|that|my)\s+(document|file|doc)\s+from\s+(before|earlier|yesterday|last time)\b|"
                     r"\bmy\s+(last|latest|recent|previous)\s+(document|file|doc|project)\b", re.I)
R_APP_ACT = re.compile(r"\b(open|launch|start|close|switch to|maximi[sz]e|minimi[sz]e|type|write|click|press|save)\b.*\b(" + APP_WORDS + r")\b|"
                       r"\b(" + APP_WORDS + r")\b.*\b(open|launch|start|close|maximi[sz]e|minimi[sz]e|type|write|click|press|save)\b", re.I)
# ======================================================================================================================
# PHONE · the iPhone simulator on the Mac (iphone.py): IO looks at its screen and taps, types and swipes on it over SSH
# ======================================================================================================================

def _phone():
    import iphone
    if not iphone.configured():
        raise Fail("NEEDS", "no iPhone simulator is set up (data/iphone.json: the Mac's host, user, SSH key and simulator udid)",
                   "ask_user(...)")
    return iphone


async def _phone_call(fn, *args, timeout: float = 120):
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout)
    except LookupError as e:
        raise Fail("NOT_FOUND", str(e), 'phone_look("what apps are on the screen?")')
    except (RuntimeError, OSError, asyncio.TimeoutError, subprocess.TimeoutExpired) as e:
        raise Fail("FAILED", f"the iPhone simulator: {e or 'no answer'}", "try again, or ask_user if the Mac is off")


def _phone_ready_sync():
    ph = _phone()
    ph.ensure_booted()
    return ph


async def _phone_shot(ctx: Ctx) -> tuple:
    """(the simulator's screen as a PIL image, its data URL sized for the eyes)."""
    from PIL import Image
    ph = await _phone_call(_phone_ready_sync, timeout=320)
    png = await _phone_call(ph.screenshot)
    img = Image.open(io.BytesIO(png)).convert("RGB")
    h = _h()
    iw, ih = h.smart_size(img.width, img.height)
    buf = io.BytesIO()
    img.resize((iw, ih)).save(buf, format="PNG")
    return img, "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@action("phone_look", group="PHONE", summary="what the iPhone simulator's screen shows, or answer a question about it",
        params="question s? what you want to know (else: describe the screen)",
        cost=8.0, tier="vision", star=True, top="question", fallback="ask_user(...)", limits="vision: can misread small text",
        timeout=360.0)
async def phone_look(ctx: Ctx, question: str = "", **_) -> str:
    from openai import OpenAI
    _img, url = await _phone_shot(ctx)
    h = _h()
    text = ("This is a screenshot of an iPhone. " + (question or "Describe what is on the screen: which app or screen is open, "
            "the exact text of the buttons and items that matter, and where they are."))
    r = await asyncio.to_thread(h.local_create, OpenAI(base_url=h.EVO_URL, api_key="local", max_retries=1, timeout=120), h.EVO_MODEL,
                                _purpose="look at the iPhone", temperature=0.2, max_tokens=1100, extra_body=h.EVO_THINK_LONG,
                                messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}},
                                                                        {"type": "text", "text": text}]}])
    answer = re.sub(r"<think>.*?</think>", "", r.choices[0].message.content or "", flags=re.S).strip()
    return ok(answer or "(the eyes said nothing)")


@action("phone_tap", group="PHONE", summary="tap something on the iPhone simulator, found by how it looks",
        params="target s what to tap, e.g. the Wi-Fi row, the blue Continue button",
        cost=8.0, tier="vision", star=True, top="target", fallback="phone_look(question)", limits="vision: can miss; look again after",
        timeout=360.0)
async def phone_tap(ctx: Ctx, target: str, **_) -> str:
    _img, url = await _phone_shot(ctx)
    h = _h()
    frac = await asyncio.to_thread(h.evo_point, eyes(ctx).client, url, target)
    if frac is None:
        raise Fail("NOT_FOUND", f"the eyes don't see {target!r} on the iPhone's screen", 'phone_look("what is on the screen?") or phone_swipe("up")')
    ph = _phone()
    x, y = await _phone_call(ph.tap, *frac)
    await asyncio.sleep(1.0)
    return ok(f"tapped {target} at ({x}, {y}) points", now="phone_look() to see what changed")


@action("phone_type", group="PHONE", summary="type text into the focused field on the iPhone simulator",
        params="text s what to type", cost=2.0, star=True, top="text", fallback="phone_tap(the key)", limits="tap the field first")
async def phone_type(ctx: Ctx, text: str, **_) -> str:
    ph = await _phone_call(_phone_ready_sync, timeout=320)
    await _phone_call(ph.type_text, text)
    return ok(f"typed {len(text)} characters")


@action("phone_swipe", group="PHONE", summary="swipe the iPhone simulator's screen: up (scrolls down), down, left, right",
        params="direction s up|down|left|right", cost=2.0, top="direction", fallback="phone_tap(...)")
async def phone_swipe(ctx: Ctx, direction: str, **_) -> str:
    d = str(direction).lower().strip()
    if d not in ("up", "down", "left", "right"):
        raise Fail("BAD_ARGS", f"direction must be up, down, left or right, not {direction!r}", 'phone_swipe("up")')
    ph = await _phone_call(_phone_ready_sync, timeout=320)
    await _phone_call(ph.swipe, d)
    await asyncio.sleep(0.6)
    return ok(f"swiped {d}", now="phone_look() to see the screen")


@action("phone_open", group="PHONE", summary="open an app (by name) or a link on the iPhone simulator",
        params="target s an app name (Settings, Safari, Photos) or a link (https://..., tel:...)",
        cost=4.0, star=True, top="target", fallback="phone_home() then phone_tap(the app icon)", timeout=360.0)
async def phone_open(ctx: Ctx, target: str, **_) -> str:
    ph = await _phone_call(_phone_ready_sync, timeout=320)
    said = await _phone_call(ph.open_target, target)
    await asyncio.sleep(1.5)
    return ok(said, now="phone_look() to see it")


@action("phone_home", group="PHONE", summary="press the iPhone simulator's Home button", cost=1.5, fallback="phone_swipe(\"up\")")
async def phone_home(ctx: Ctx, **_) -> str:
    ph = await _phone_call(_phone_ready_sync, timeout=320)
    await _phone_call(ph.home)
    return ok("pressed Home")


R_PHONE = re.compile(r"\b(iphone|ipad|ios|simulator|sim)\b", re.I)
R_SCREEN = re.compile(r"on (my|the) (main |primary |second |other |left |right )?(screen|monitor|display)|what do you see|"
                      r"look at (my|the) screen|is .* (open|visible) on|what'?s on screen", re.I)
R_SETTINGS = re.compile(r"settings?\b|dark mode|night light|bluetooth|wallpaper|resolution|brightness|notifications|default app", re.I)
R_PCFACTS = re.compile(r"installed|do i have|what time|time in|\bdisk\b|free space|storage|\bcpu\b|\bgpu\b|\bram\b|memory usage|ip address|uptime|"
                       r"battery|processes|running|what('s| is) open|apps (are )?open|\bwifi\b|wi-fi|windows version|steam games|cores", re.I)
R_ACT_VERB = re.compile(r"(?:^|[.,;!?]\s*|\b(?:and|then|please|to|can you|could you|now|also)\s+)(open|launch|start|click|type|write|close)\s+\w", re.I)
R_NEGATED = re.compile(r"\b(?:don'?t|do not|never|without|not)\s+(?:\w+\s+)?(open|launch|start|click|type|write|close)", re.I)
R_WEB = re.compile(r"https?://|\bwww\.|\b[\w-]+\.(com|org|net|io|ai|gov|edu|co|uk|de|dev|app|me|tv|info|wiki)\b|\bsearch\b|\bgoogle\b|look up|"
                   r"website|web ?page|\bonline\b|price of|weather|\bnews\b|wikipedia|in (your|the) (browser|tab)", re.I)
R_APP = re.compile(r"\b(open|launch|start|close|switch to|maximi[sz]e|minimi[sz]e|type|write|click|press|save)\b|\b(" + APP_WORDS + r")\b", re.I)
R_KNOWLEDGE = re.compile(r"^(what|who|when|where|why|how|which|is|are|does|do|can|define|explain)\b", re.I)
R_PERSONAL = re.compile(r"\b(my|this pc|computer|window|screen|file|folder|" + APP_WORDS + r")\b", re.I)

LOOP_MENU = ["steps", "click_on", "hold_on", "look_at_screen", "wait", "game_state", "tap_repeatedly", "hold_until", "swipe", "close_popups",
             "check_screen", "Scroll", "Shortcut", "type_text", "research", "ask_model", "done"]
ROUTES = {
    "images": Route("images", ["done"], [], "local", None, "no"),
    "loop": Route("loop", LOOP_MENU, ["GAME", "SEE"], "director", 0, "as_today"),
    "chat": Route("chat", ["done"], [], "local", None, "no"),
    "files": Route("files", ["list_files", "find_file", "read_file", "write_file", "edit_file", "file_op", "open_path", "open_file", "run_command", "start_app",
                             "api_lookup", "PowerShell", "ask_user", "done"],
                   ["FILE"], "local", 2, "no"),
    "screen": Route("screen", ["look_at_screen", "list_windows", "read_window", "check_screen", "done"], ["SEE", "READ"], "local", 1, "no"),
    "settings": Route("settings", ["change_setting", "open_settings", "set_control", "find_control", "read_window", "scroll_until", "click", "list_controls", "done"],
                      ["ACT", "READ"], "local", 2, "if_director_off"),
    "pc_facts": Route("pc_facts", ["pc_info", "app_info", "list_windows", "calc", "find_file", "read_file", "PowerShell", "done"],
                      ["PC"], "local", 2, "no"),
    "web": Route("web", ["web_answer", "web_search", "read_page", "web_click", "web_fill", "research", "done"], ["WEB"], "local", 2, "if_director_off"),
    "phone": Route("phone", ["phone_look", "phone_tap", "phone_type", "phone_swipe", "phone_open", "phone_home", "done"],
                   ["PHONE"], "director", 0, "if_director_off"),
    "app": Route("app", ["open_app", "click", "type_into", "read_window", "select_menu", "hotkeys", "write_in_app", "save_file_as", "close_window",
                         "calculator", "list_controls", "window_state", "open_path", "done"], ["WIN", "ACT", "DO"], "director", 0, "if_director_off"),
    # a request that doesn't say which file, window or thing ("open the document I was working on"): the local model asks
    # first (the director, guessing from Recent items, opened the user's files on its own)
    "vague": Route("vague", ["ask_user", "list_windows", "list_files", "find_file", "read_file", "open_path", "open_app", "read_window",
                             "write_in_app", "type_into", "done"],
                   ["WIN", "FILE"], "local", 99, "no"),  # 99: not on failures; the director takes over once the user has answered
    "knowledge": Route("knowledge", ["calc", "web_answer", "web_search", "read_page", "done"], ["WEB"], "local", None, "no"),
    "general": Route("general", ["open_app", "click", "type_into", "read_window", "list_windows", "window_state", "pc_info", "app_info", "find_file",
                                 "web_answer", "look_at_screen", "PowerShell", "done"], ["WIN", "ACT"], "director", 0, "if_director_off"),
}


def _clean(text: str) -> str:
    return (text or "").replace("’", "'").replace("‘", "'").strip()


def route_of(text: str, loop: bool = False, images: bool = False) -> Route:
    """The route for a request, by regex rules in priority order (the first hit wins). A wrong route is cheap: every
    menu has tools()/use(), and try: hints name actions in other groups."""
    t = _clean(text)
    if images:
        return ROUTES["images"]
    if loop:
        return ROUTES["loop"]
    if (SMALL_TALK.match(t) and len(t.split()) <= 6) or R_ABOUT.search(t):
        return ROUTES["chat"]
    if R_VAGUE.search(t) and not R_PATH.search(t) and not re.search(r"[\"'“][^\"'”]{2,}[\"'”]", t):
        return ROUTES["vague"]
    app_act = bool(R_APP_ACT.search(t)) or bool(R_WINDOW_THING.search(t))
    if (R_PATH.search(t) or (R_FILEWORDS.search(t) and not R_IN_APP.search(t))) and not app_act:
        return ROUTES["files"]
    if R_PHONE.search(t):
        return ROUTES["phone"]
    if R_SCREEN.search(t):
        return ROUTES["screen"]
    if R_SETTINGS.search(t):
        return ROUTES["settings"]
    if R_PCFACTS.search(t):
        acts = [m.group(1).lower() for m in R_ACT_VERB.finditer(t)]
        negated = {m.group(1).lower() for m in R_NEGATED.finditer(t)}
        if not [a for a in acts if a not in negated]:
            return ROUTES["pc_facts"]
    if R_WEB.search(t):
        return ROUTES["web"]
    if R_APP.search(t):
        return ROUTES["app"]
    if R_KNOWLEDGE.search(t) and not R_PERSONAL.search(t):
        return ROUTES["knowledge"]
    return ROUTES["general"]


# ======================================================================================================================
# constraints ("dont open it"): regex only, enforced in one place
# ======================================================================================================================

C_NO_LAUNCH = re.compile(r"\b(?:don'?t|dont|do not|never|without)\s+(?:\w+\s+)?(?:open|launch|start|run)(?:ing)?\s+(it|that|them|this|[\w .-]{2,30}?)"
                         r"(?=[.,;!?]|$|\s+(?:and|but|just|please|or)\b)", re.I)
C_CHECK_ONLY = re.compile(r"\b(do i have|is .{1,40} installed|check if .{1,60} installed|is .{1,30} on (my|this) (pc|computer))\b", re.I)
C_NO_SAVE = re.compile(r"\b(?:don'?t|dont|do not|without)\s+sav(?:e|ing)\b", re.I)
C_NO_CLOSE = re.compile(r"\b(?:don'?t|dont|do not)\s+close(?:\s+(it|that|them|the\s+[\w .-]{2,30}?|[\w.-]{2,30}))?(?=[.,;!?]|$|\s+(?:and|but)\b)", re.I)
C_NO_BROWSER = re.compile(r"\b(?:don'?t|dont|do not|without)\s+(?:using\s+|use\s+)?(?:the\s+|a\s+|any\s+)?(browser|chrome|internet|web)\b", re.I)
C_NO_ASK = re.compile(r"\b(?:don'?t|dont|do not|without)\s+ask(?:ing)?(?:\s+me)?\b", re.I)
PRONOUNS = {"it", "that", "them", "this", ""}


# a request that does something of its own: earlier turns' "don't ..." no longer apply to it ("check if i have runescape
# installed. dont open it", then "open notepad and write hi" must open Notepad)
C_OWN_ACTION = re.compile(r"\b(open|launch|start|run|close|save|write|type|click|delete|rename|move|copy|play|install|maximi[sz]e|"
                          r"minimi[sz]e|search|go to|browse|ask)\b", re.I)
C_OPPOSITE = {"no_launch": r"\b(open|launch|start|run|play)\b", "no_save": r"\bsav(e|ing)\b", "no_close": r"\bclos(e|ing)\b",
              "no_browser": r"\b(browser|chrome|web|online|search|google)\b", "no_ask": r"\bask\b"}


def _constraints_in(t: str) -> list[tuple[str, str]]:
    out: list = []

    def add(kind: str, target: str) -> None:
        target = (target or "").strip().lower()
        target = "*" if target in PRONOUNS else re.sub(r"^the\s+", "", target)
        if (kind, target) not in out:
            out.append((kind, target))

    for m in C_NO_LAUNCH.finditer(t):
        add("no_launch", m.group(1))
    if C_CHECK_ONLY.search(t) and not R_ACT_VERB.search(re.sub(C_NO_LAUNCH, "", t)):
        add("no_launch", "*")
    if C_NO_SAVE.search(t):
        add("no_save", "*")
    for m in C_NO_CLOSE.finditer(t):
        add("no_close", m.group(1) or "")
    if C_NO_BROWSER.search(t):
        add("no_browser", "*")
    if C_NO_ASK.search(t):
        add("no_ask", "*")
    return out


def constraints_of(request: str, conversation: list | None = None) -> list[tuple[str, str]]:
    """[(kind, target)] from the request; target '*' means any (a pronoun or no name). The user's earlier turns count
    only for a follow-up that asks nothing of its own ("runelite?" after "check if i have runescape installed. dont open
    it"): a request with its own action verb ("open notepad and write hi", "save it as todo.txt") is governed by its own
    words, and an earlier constraint never blocks the very thing the new request asks for."""
    t = _clean(request)
    out = _constraints_in(t)
    if C_OWN_ACTION.search(re.sub(C_NO_LAUNCH, "", t)):
        return out
    earlier = [_clean(m["content"]) for m in (conversation or [])[-12:]
               if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str)][-3:]
    for e in reversed(earlier):  # newest first: the nearest turn sets the context of a follow-up
        for kind, target in _constraints_in(e):
            if re.search(C_OPPOSITE[kind], re.sub(C_NO_LAUNCH, "", t), re.I):
                continue  # the new request asks for exactly this
            if (kind, target) not in out:
                out.append((kind, target))
        if out:
            break
    return out


def constraints_text(constraints: list) -> str:
    words = {"no_launch": "don't open or launch {t}", "no_save": "don't save", "no_close": "don't close {t}",
             "no_browser": "don't use the browser", "no_ask": "don't ask the user"}
    return "; ".join(words[k].format(t="any app" if t == "*" else t) for k, t in constraints) if constraints else ""


def _hits(target: str, *values) -> bool:
    if target == "*":
        return True
    t = _squash(target)
    return any(t and t in _squash(str(v or "")) for v in values)


CLOSE_KEYS = re.compile(r"^\s*(alt\s*\+\s*f4|ctrl\s*\+\s*(w|f4)|ctrl\s*\+\s*shift\s*\+\s*w)\s*$", re.I)  # close a window or tab


def _keys_of(args: dict) -> list[str]:
    """hotkeys' keys / Shortcut's shortcut as a list of combos, however the model sent them ("ctrl+s", ["ctrl+s"], 5)."""
    keys = args.get("keys")
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split(",")] if not keys.strip().startswith("[") else _loads_list(keys)
    elif not isinstance(keys, (list, tuple)):
        keys = [keys] if keys not in (None, "") else []
    out = [str(k) for k in keys]
    if args.get("shortcut") not in (None, ""):
        out.append(str(args["shortcut"]))
    return out


def _loads_list(s: str) -> list:
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else [s]
    except ValueError:
        return [s]


def closes_by_keys(name: str, args: dict) -> bool:
    """alt+f4 / ctrl+w through hotkeys or Shortcut: a close like close_window, so it gets the same checks."""
    return name in ("hotkeys", "Shortcut") and any(CLOSE_KEYS.match(k) for k in _keys_of(args if isinstance(args, dict) else {}))


def constraint_block(name: str, args: dict, ctx: Ctx) -> str:
    """'' if the call is fine, else the result to return instead: error:BLOCKED naming the user's own words. Never raises:
    arguments arrive raw (a string where a list belongs, a number), so they are coerced first."""
    try:
        args = args if isinstance(args, dict) else {}
        a = REGISTRY.get(name)
        if a is not None:
            coerced, _problem = _coerce(a, args)
            args = {**args, **coerced}
        return _constraint_block(name, args, ctx)
    except Exception:
        return ""


def _constraint_block(name: str, args: dict, ctx: Ctx) -> str:
    for kind, target in ctx.constraints or []:
        who = "any app" if target == "*" else target
        if kind == "no_launch":
            launch = None
            if name == "open_app":
                launch = args.get("name")
            elif name == "App" and str(args.get("mode", "launch")) in ("launch", "launch_executable"):
                launch = args.get("name") or args.get("executable")
            elif name == "open_path" and (args.get("app") or LAUNCHABLE.search(str(args.get("path", "")))):
                launch = args.get("app") or args.get("path")
            elif name in ("write_in_app", "calculator") and not [w for w in windows() if _app_match(str(args.get("app") or "calculator"))(w)]:
                launch = args.get("app") or "Calculator"
            elif name == "PowerShell" and re.search(r"\b(Start-Process|saps|start|Invoke-Item|ii)\b", str(args.get("command", "")), re.I):
                launch = args.get("command")
            if launch is not None and _hits(target, launch):
                return err("BLOCKED", f"the user said not to open {who}", f'app_info("{str(launch)[:40]}")')
        elif kind == "no_save":
            keys = " ".join(_keys_of(args))
            if (name == "save_file_as" or (name in ("hotkeys", "Shortcut") and re.search(r"ctrl\s*\+\s*(shift\s*\+\s*)?s\b", keys, re.I))
                    or (name == "write_file" and args.get("mode") == "overwrite") or (name == "write_in_app" and args.get("save_to"))
                    or (name == "close_window" and args.get("unsaved") == "save")):
                return err("BLOCKED", "the user said not to save", "leave it unsaved, or done")
        elif kind == "no_close":
            titles = args.get("titles") if isinstance(args.get("titles"), list) else [args.get("titles") or ""]
            if name in ("close_window", "close_windows") or (name == "Process" and args.get("mode") == "kill") or closes_by_keys(name, args):
                what = " ".join(map(str, [args.get("window", ""), *titles, args.get("name", "")]))
                if closes_by_keys(name, args) and not str(args.get("window") or ""):
                    w = resolve(ctx, "")  # keys go to the task's window
                    what += " " + (w.title if w else "")
                if _hits(target, what) or name == "close_windows" and args.get("all_except") is not None:
                    return err("BLOCKED", f"the user said not to close {who}", "done")
        elif kind == "no_browser":
            a = REGISTRY.get(name)
            if ((a and a.group == "WEB") or name.startswith("browser_") or name in ("web_answer", "research", "Scrape")
                    or (name == "fill_form" and str(args.get("window", "")).lower() == "web")):
                return err("BLOCKED", "the user said not to use the browser", "answer from what you know, or ask_user")
        elif kind == "no_ask" and name == "ask_user":
            return ok("the user said not to ask; decide yourself")
    return ""


def risky(name: str, args: dict, ctx: Ctx) -> str:
    """Why this registry action needs the user's OK ('' if it doesn't). boss also runs its own risky_reason."""
    a = REGISTRY.get(name)
    args = args if isinstance(args, dict) else {}
    try:
        if name == "Shortcut" and closes_by_keys(name, args):  # the L1 twin of hotkeys(["alt+f4"])
            return _keys_close_risky(args, ctx)
        if a is None or a.risky is None:
            return ""
        coerced, _problem = _coerce(a, args)
        return a.risky({**args, **coerced}, ctx) or ""
    except Exception:
        return ""


# ======================================================================================================================
# catalogs, menus, planner and SYSTEM text: all generated from the registry
# ======================================================================================================================

DIRECTOR_STANDING = ('You direct IO, an agent on a Windows PC that runs your actions exactly. Reply ONLY with one ```json block: '
                     '{"thoughts":"1 sentence","plan":"short plan","actions":[{"tool":"name","args":{},"expect":"what you should see"}]}, '
                     '1-8 actions. Results start ok:, unsure: or error:CODE: with a try: hint; follow it. Prefer exact actions over click_on. '
                     'tools(group) lists more. Finish with done(summary).')


def available(ctx: Ctx | None = None, *, loop: bool | None = None, decider: str = "local", browser: bool | None = None,
              ask: bool | None = None, research: bool = True) -> list[str]:
    """Names offered this task, in registry order: browser actions only with a browser, GAME helpers only in loops,
    ask_user only in single tasks that can ask, internal and disabled actions never."""
    loop = bool(ctx.loop) if loop is None and ctx is not None else bool(loop)
    browser = (ctx.browser is not None) if browser is None and ctx is not None else (True if browser is None else browser)
    ask = (ctx.ask is not None) if ask is None and ctx is not None else (True if ask is None else ask)
    kind = "loop" if loop else "single"
    out = []
    for name, a in REGISTRY.items():
        if "internal" in a.modes or disabled(name) or kind not in a.modes or decider not in a.modes:
            continue
        if not browser and (a.group == "WEB" or name.startswith("browser_") or name == "web_answer"):
            continue
        if name == "ask_user" and (loop or not ask):
            continue
        if name == "research" and not research:
            continue
        out.append(name)
    return out


def _cost(c: float) -> str:
    return "instant" if c < 0.05 else f"~{c:.1f}s" if c < 1 else f"~{c:.0f}s"


def catalog_group(group: str, avail: list | None = None, limit: int = 900) -> str:
    """One group expanded: one line per action with its arguments, typical cost, what it does and its limits (<= 900 chars)."""
    g = group.upper()
    names = [n for n, a in REGISTRY.items() if a.group == g and "internal" not in a.modes and not disabled(n) and (avail is None or n in avail)]
    if not names:
        return f"{g}: nothing available here"

    def build(with_limits: bool, cut: int, skip: tuple) -> str:
        head = GROUP_HEAD.get(g, g) + (" [window?/expect? not shown]" if skip else "") + ":"
        lines = [head]
        for n in names:
            a = REGISTRY[n]
            if cut == 0:
                lines.append(f"- {a.signature(skip=skip)}")
                continue
            summary = a.summary if len(a.summary) <= cut else a.summary[:cut].rsplit(" ", 1)[0] + "…"
            line = f"- {a.signature(skip=skip)} {_cost(a.cost)}: {summary}"
            if with_limits and a.limits:
                line += f"; {a.limits}"
            lines.append(line)
        return "\n".join(lines)

    lean = ("window", "expect", "display")
    stages = ((True, 200, ()), (True, 200, lean), (False, 200, ()), (False, 200, lean), (False, 60, lean), (False, 40, lean), (False, 0, lean))
    for with_limits, cut, skip in stages:
        text = build(with_limits, cut, skip)
        if len(text) <= limit:
            return text
    # signatures only, packed: everything is still named, details come from the error hints
    packed = GROUP_HEAD.get(g, g) + ": " + " ".join(REGISTRY[n].signature(skip=lean) for n in names)
    return packed[:limit]


def catalog_top(avail: list | None = None, loop: bool = False, limit: int = 1300) -> str:
    """The director's top level: the starred actions per group, required args (plus one optional) only. In loops: GAME and
    SEE expanded instead."""
    if loop:
        return "\n".join([catalog_group("GAME", avail), catalog_group("SEE", avail), "END research(question) done(summary)"])
    lines = ['Actions: name(args). !=asks the user first. IO fills window= with the app it works in. tools("GROUP") shows details.']
    for g in GROUPS:
        if g in ("SEE", "GAME", "RAW"):
            continue
        acts = [a for n, a in REGISTRY.items() if a.group == g and a.star and "internal" not in a.modes and not disabled(n)
                and (avail is None or n in avail)]
        if acts:
            lines.append(g + " " + " ".join(a.signature(full=False) + ("!" if a.bang else "") for a in acts))
    more = [x for x in ("SEE (vision)", "GAME", "RAW (Snapshot, Click, PowerShell, browser_*)")
            if any(a.group == x.split()[0] and (avail is None or n in avail) for n, a in REGISTRY.items())]
    if more:
        lines.append("more groups: " + " ".join(more))
    text = "\n".join(lines)
    return text if len(text) <= limit else text[:limit]


# parameters whose name says it all: their descriptions would only spend the local model's context
QUIET = {"window", "text", "question", "summary", "enter", "timeout", "args", "note", "query", "name"}


def tool_schema(name: str) -> dict:
    """A compact OpenAI function definition. expect= is the director's; the local model checks with READ actions."""
    a = REGISTRY[name]
    props = {}
    for k, v in a.params.items():
        if k == "expect" or k in a.hide:
            continue
        p = {kk: vv for kk, vv in v.items() if kk != "description"}
        if v.get("description") and (k not in QUIET or a.fn is None and k == "name"):
            p["description"] = v["description"][:40]
        props[k] = p
    desc = a.summary + (" " + a.desc if a.desc else "")
    params = {"type": "object", "properties": props}
    if a.required:
        params["required"] = list(a.required)
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": params}}


def openai_tools(names: list, defs: dict | None = None) -> list[dict]:
    """OpenAI function definitions for the local model: compact registry schemas (summary <= 70 chars, parameter text
    <= 40), or the caller's own definition for names the registry doesn't know (plugins)."""
    out = []
    for n in dict.fromkeys(names):
        if n in REGISTRY:
            out.append(tool_schema(n))
        elif defs and n in defs:
            out.append(defs[n])
    return out


def tools_chars(names: list, defs: dict | None = None) -> int:
    return len(json.dumps(openai_tools(names, defs), ensure_ascii=False))


def menu(route: Route, avail: list | None = None, ask: bool = True) -> list[str]:
    """The local model's tools for a route: the route's menu (14 at most outside loops), then done, tools, use and ask_user."""
    names = [n for n in route.menu if avail is None or n in avail]
    if route.kind != "loop":
        names = names[:14]
    always = ["done", "tools", "use"] + (["ask_user"] if ask and route.kind != "loop" else [])
    return list(dict.fromkeys(names + [n for n in always if avail is None or n in avail]))


ESCALATION_TOOLS = ["click_on", "find_on_screen", "look_at_screen", "Snapshot"]


def escalate(names: list, failures: int, avail: list | None = None) -> tuple[list, list]:
    """After 2 NOT_FOUND/UNSUPPORTED results the vision tools join the menu: (new menu, added names)."""
    if failures < 2:
        return names, []
    added = [n for n in ESCALATION_TOOLS if n not in names and (avail is None or n in avail)]
    return names + added, added


def planner_text(names: list) -> str:
    """The planner's tool paragraph, from the same registry."""
    lines = ["Tools the agent has (prefer the first that fits; each checks its own result):"]
    for n in names:
        if n in REGISTRY and n not in ("tools", "use"):
            a = REGISTRY[n]
            lines.append(f"- {a.signature(full=False) if a.top is not None else a.signature()}: {a.summary}")
    lines.append("- tools(group) lists more actions; use(name, args) runs one of them.")
    return "\n".join(lines)


LADDER = [  # intent, first, then, last (4.12): what the menus' order and SYSTEM teach
    ("a fact about the PC", ["pc_info", "app_info", "list_windows"], ["PowerShell"], ["look_at_screen"]),
    ("maths", ["calc"], [], []),
    ("maths in the Calculator app (only when asked for the app)", ["calculator"], ["click"], []),
    ("which file or window, when the request doesn't say", ["ask_user"], ["list_windows", "list_files"], []),
    ("a folder or file shown in its app (File Explorer to Downloads)", ["open_path"], [], []),
    ("text in a window", ["read_window"], ["read_region"], ["look_at_screen"]),
    ("a click", ["click"], ["click(careful=true)"], ["find_on_screen", "Click"]),
    ("typing into a field", ["type_into"], ["click", "type_text"], []),
    ("a menu command", ["hotkeys"], ["select_menu"], ["click"]),
    ("waiting for the UI", ["wait_until"], [], ["wait"]),
    ("files", ["list_files", "find_file", "read_file", "write_file", "file_op"], ["open_file", "open_path"], []),
    ("a setting", ["change_setting", "open_settings", "set_control"], ["click", "read_window"], ["ask_user"]),
    ("a web fact", ["web_answer"], ["web_search", "read_page"], ["research"]),
    ("a page element", ["web_click", "web_fill"], ["browser_snapshot", "browser_click"], []),
    ("a game action", ["tap_repeatedly", "hold_until", "click_on"], ["find_all", "Click"], ["look_at_screen", "research"]),
    ("the game's state", ["game_state"], ["check_screen"], ["look_at_screen"]),
]


def system_tools_text(names: list) -> str:
    """'PICK THE RIGHT TOOL' for the local model's SYSTEM prompt: the preference ladder limited to the menu."""
    have = set(names)
    base = lambda x: x.split("(")[0]
    lines = ["PICK THE RIGHT TOOL (cheapest reliable one first; results start ok:, unsure: or error:CODE: with a try: hint)"]
    for intent, first, then, last in LADDER:
        f = [x for x in first if base(x) in have]
        if not f:
            continue
        rest = [x for x in then if base(x) in have]
        tail = [x for x in last if base(x) in have]
        line = f"- {intent}: {', '.join(f)}" + (f", else {', '.join(rest)}" if rest else "") + (f", last {', '.join(tail)}" if tail else "")
        lines.append(line)
    if "calc" in have:
        lines.append("- calc never opens Calculator; drive the Calculator app only when the user asks for it" +
                     (" (calculator(expression) does it in one step)." if "calculator" in have else "."))
    lines.append("- If no tool fits, call tools(group) and then use(name, args).")
    return "\n".join(lines)


# ======================================================================================================================
# selftest: python actions.py --selftest [--no-desktop] [--vision] [--recycle]
# ======================================================================================================================

ROUTE_CASES = [  # every bench prompt (bench/tasks.json) and the route it must take
    ("hello", "chat"), ("what's the capital of Australia?", "knowledge"), ("who are you?", "chat"), ("What is 17% of 2,340?", "knowledge"),
    ("What time is it in Tokyo?", "pc_facts"), ("How much free space is on my C drive?", "pc_facts"),
    ("can you check if i have runescape installed. dont open it", "pc_facts"), ("can you check if I have RuneLite installed? don't open it", "pc_facts"),
    ("What apps do I have open right now?", "pc_facts"), ("How many CPU cores do I have and what GPU?", "pc_facts"),
    ("What's my local IP address?", "pc_facts"), ("List my installed Steam games", "pc_facts"),
    (r"Use PowerShell to count the files in C:\Windows\System32\drivers\etc", "files"),
    (r"List the 5 newest files in %TEMP%\io-bench\newest", "files"), (r"Find the file quarterly-report-x7.txt in %TEMP%\io-bench", "files"),
    (r"Create %TEMP%\io-bench\todo.txt containing buy milk", "files"), (r"What does %TEMP%\io-bench\notes.txt say?", "files"),
    (r"Delete everything in %TEMP%\io-bench\trash", "files"), (r"How big is the %TEMP%\io-bench\big folder?", "files"),
    ("Open Calculator", "app"), ("Open Notepad and write: eggs, milk, bread", "app"), ("Close Calculator", "app"),
    ("Calculate 1234 times 5678 in the Calculator app", "app"), ("Open File Explorer to my Downloads folder", "app"),
    ("Open the display settings", "settings"), ("Maximize the Notepad window", "app"), ("What text is in the Notepad window?", "app"),
    (r"Open Notepad, write hello bench, and save it as %TEMP%\io-bench\saved.txt", "app"),
    ("Open example.com in your browser tab and tell me the page title", "web"),
    ("On Wikipedia, what is the diameter of Io, Jupiter's moon?", "web"), ("When was Python 3.13 released?", "knowledge"),
    ("What is duck.ai?", "web"), ('Open example.com and click the "More information" link (or "Learn more"). Where does it go?', "web"),
    ("What's on my main monitor right now?", "screen"), ("Is Calculator open? Look at the screen.", "screen"),
    ("Open the document I was working on", "vague"), ("Write the numbers 1 to 200 in Notepad, one per line", "app"),
    ("move this window to my left monitor", "general"), ("copy the text from notepad into a new file", "app"),
    ("Is RuneLite installed?", "pc_facts"), ("Can you check if I have RuneLite installed?", "pc_facts"),
]
CONSTRAINT_CASES = [
    ("can you check if i have runescape installed. dont open it", [], {("no_launch", "*")}),
    ("runelite?", [{"role": "user", "content": "can you check if i have runescape installed. dont open it"}], {("no_launch", "*")}),
    ("write a note in notepad but don't save it", [], {("no_save", "*")}),
    ("rename the files without asking me", [], {("no_ask", "*")}),
    ("open notepad and type hi", [], set()),
    ("look it up but don't use the browser", [], {("no_browser", "*")}),
    ("don't close discord", [], {("no_close", "discord")}),
    # earlier turns never block a new request's own action
    ("open notepad and write hi", [{"role": "user", "content": "can you check if i have runescape installed. dont open it"}], set()),
    ("save it as todo.txt", [{"role": "user", "content": "type hello in notepad, dont save it"}], set()),
    ("Can you check if I have RuneLite installed?", [{"role": "user", "content": "can you check if i have runescape installed. dont open it"}],
     {("no_launch", "*")}),
]
CALC_CASES = [("17% of 2340", "397.8"), ("1234 * 5678", "7006652"), ("1234 times 5678", "7006652"), ("(2+3)^2", "25"), ("sqrt(144)", "12"),
              ("days between 2026-01-01 and 2026-10-03", "275 days"), ("5 km in miles", "3.10685"), ("100 f to c", "37.78"),
              ("2,340 / 4", "585")]


async def _selftest(desktop: bool = True, vision: bool = False, recycle: bool = False) -> list[tuple[str, bool, str]]:
    results: list = []

    def check(name: str, passed: bool, evidence: str) -> None:
        results.append((name, bool(passed), str(evidence)[:300]))
        print(f"{'PASS' if passed else 'FAIL'}  {name}: {str(evidence)[:200]}".encode("ascii", "replace").decode(), flush=True)

    ctx = Ctx(options={}, request="IO action selftest")
    form = lambda r: bool(isinstance(r, str) and RESULT.match(r))

    # --- contract ---
    natives = [n for n, a in REGISTRY.items() if a.fn is not None]
    externals = [n for n, a in REGISTRY.items() if a.fn is None]
    check("registry", len(natives) >= 49 and len(externals) >= 20, f"{len(natives)} native, {len(externals)} external")
    bad = []
    for n in natives:
        a = REGISTRY[n]
        if not a.required:
            continue
        r = await call(n, {}, Ctx(request="x"))
        if not (form(r) and r.startswith("error:BAD_ARGS")):
            bad.append(f"{n}: {r[:60]}")
    check("bad args -> error:BAD_ARGS", not bad, "; ".join(bad) or f"{sum(1 for n in natives if REGISTRY[n].required)} actions checked")

    async def boom(ctx, **_):
        raise RuntimeError("forced")
    REGISTRY["_boom"] = Action("_boom", 2, "PC", "test", {}, (), boom, 0, "exact", modes=frozenset({"internal"}))
    r = await call("_boom", {}, ctx)
    del REGISTRY["_boom"]
    check("forced exception -> error:UNSUPPORTED", r.startswith("error:UNSUPPORTED: RuntimeError"), r)
    check("unknown action", (await call("clik", {}, ctx)).startswith("error:BAD_ARGS"), await call("clik", {}, ctx))
    bad_summaries = [n for n, a in REGISTRY.items() if len(a.summary) > 70 or any(len(v.get("description", "")) > 40 for v in a.params.values())]
    check("summaries <= 70, param text <= 40", not bad_summaries, ", ".join(bad_summaries) or "all fit")
    saved = dict(_settings_cache)
    _settings_cache.update(t=time.time() + 3600, v={"enabled": True, "disabled": ["calc"]})
    r = await call("calc", {"expr": "1+1"}, ctx)
    gone = "calc" not in available(ctx) and "calc(" not in catalog_top()
    _settings_cache.clear()
    _settings_cache.update(saved)
    check("disabled action", r.startswith("error:UNSUPPORTED: disabled") and gone, r)
    fails = Ctx(request="x")
    r1 = await call("read_file", {"path": r"C:\nope\missing.txt"}, fails)
    r2 = await call("read_file", {"path": r"C:\nope\missing.txt"}, fails)
    r3 = await call("read_file", {"path": r"C:\nope\missing.txt"}, fails)
    check("rut: 2nd time, then REFUSED", "(2nd time)" in r2 and r3.startswith("error:REFUSED"), f"{r2[:60]} / {r3[:60]}")

    # --- catalogs ---
    top = catalog_top(available(decider="director"))
    check("catalog_top <= 1300", len(top) <= 1300, f"{len(top)} chars")
    sizes = {g: len(catalog_group(g)) for g in GROUPS}
    check("catalog_group <= 900 each", all(v <= 900 for v in sizes.values()), sizes)
    check("DIRECTOR_STANDING <= 500", len(DIRECTOR_STANDING) <= 500, f"{len(DIRECTOR_STANDING)} chars")
    budget = {}
    for k, rt in ROUTES.items():
        if k != "loop":
            budget[k] = tools_chars(menu(rt, available(decider="local")))
    check("openai_tools(menu) <= 6000 per non-loop route", all(v <= 6000 for v in budget.values()), budget)
    loop_top = catalog_top(available(loop=True, decider="director"), loop=True)
    check("loop catalog", "game_state" in loop_top and "click_on" in loop_top, f"{len(loop_top)} chars")
    pt = planner_text(menu(ROUTES["app"]))
    st = system_tools_text(menu(ROUTES["pc_facts"]))
    check("planner/system text", "open_app" in pt and "pc_info" in st and "use(name, args)" in st, f"{len(pt)} + {len(st)} chars")

    # --- routing and constraints ---
    wrong = [f"{t[:40]!r}: {route_of(t).kind} != {want}" for t, want in ROUTE_CASES if route_of(t).kind != want]
    check("route_of bench prompts", not wrong, "; ".join(wrong) or f"{len(ROUTE_CASES)} prompts")
    check("route_of loop/images", route_of("x", loop=True).kind == "loop" and route_of("x", images=True).kind == "images", "ok")
    cwrong = []
    for text, conv, want in CONSTRAINT_CASES:
        got = set(constraints_of(text, conv))
        if got != want:
            cwrong.append(f"{text[:30]!r}: {got} != {want}")
    check("constraints_of", not cwrong, "; ".join(cwrong) or f"{len(CONSTRAINT_CASES)} cases")
    cctx = Ctx(request="check if runescape is installed", constraints=[("no_launch", "*")])
    b1 = constraint_block("open_app", {"name": "RuneLite"}, cctx)
    b2 = constraint_block("App", {"mode": "launch", "name": "RuneLite"}, cctx)
    b3 = constraint_block("app_info", {"name": "RuneLite"}, cctx)
    b4 = constraint_block("ask_user", {"question": "?"}, Ctx(constraints=[("no_ask", "*")]))
    check("constraint_block", b1.startswith("error:BLOCKED") and b2.startswith("error:BLOCKED") and b3 == "" and b4.startswith("ok:"), f"{b1[:60]} | {b4}")

    # --- exact actions (read-only, or inside a temp folder of our own) ---
    cwrong = []
    for expr, want in CALC_CASES:
        r = await call("calc", {"expr": expr}, ctx)
        if want not in r.replace(",", ""):
            cwrong.append(f"{expr}: {r}")
    check("calc", not cwrong, "; ".join(cwrong) or f"{len(CALC_CASES)} cases")
    for topic in ("time", "date", "os", "cpu", "gpu", "ram", "disk", "battery", "uptime", "ip", "wifi", "displays", "processes", "network"):
        t0 = time.time()
        r = await call("pc_info", {"topic": topic}, ctx)
        check(f"pc_info {topic}", r.startswith("ok:"), f"{time.time() - t0:.2f}s {r[:150]}")
    r = await call("pc_info", {"topic": "time", "place": "Tokyo"}, ctx)
    check("pc_info time Tokyo", r.startswith("ok:") and "Tokyo" in r, r)
    t0 = time.time()
    r = await call("app_info", {"name": "notepad"}, ctx)
    check("app_info notepad", r.startswith("ok:") and "installed" in r and "not launched" in r, f"{time.time() - t0:.2f}s {r[:160]}")
    r = await call("app_info", {"name": "zzqx-not-an-app"}, ctx)
    check("app_info missing app", r.startswith("ok:") and "doesn't look installed" in r, r[:160])
    r = await call("app_info", {"name": "steam games"}, ctx)
    check("app_info steam games", r.startswith("ok:"), r[:160])
    r = await call("list_windows", {}, ctx)
    check("list_windows", r.startswith("ok:"), r[:200])

    sandbox = Path(os.environ["TEMP"]) / f"io-actions-selftest-{os.getpid()}"
    sandbox.mkdir(parents=True, exist_ok=True)
    try:
        fctx = Ctx(request=f"selftest in {sandbox}")
        f = sandbox / "notes.txt"
        r = await call("write_file", {"path": str(f), "text": "violet-kangaroo-42\nsecond line"}, fctx)
        check("write_file new", r.startswith("ok:") and f.exists(), r)
        r = await call("write_file", {"path": str(f), "text": "x"}, fctx)
        check("write_file refuses to replace", r.startswith("error:BLOCKED"), r)
        r = await call("write_file", {"path": str(f), "text": "\nthird", "mode": "append"}, fctx)
        r2 = await call("read_file", {"path": str(f), "find": "kangaroo"}, fctx)
        check("write_file append + read_file find", r.startswith("ok:") and "violet-kangaroo-42" in r2, r2[:120])
        r = await call("write_file", {"path": r"C:\Windows\io-test.txt", "text": "x"}, fctx)
        check("write_file blocked under C:\\Windows", r.startswith("error:BLOCKED"), r)
        r = await call("file_op", {"op": "copy", "src": str(f), "dst": str(sandbox / "copy.txt")}, fctx)
        r2 = await call("file_op", {"op": "rename", "src": str(sandbox / "copy.txt"), "dst": "renamed.txt"}, fctx)
        r3 = await call("file_op", {"op": "mkdir", "src": str(sandbox / "sub" / "deeper")}, fctx)
        r4 = await call("file_op", {"op": "move", "src": str(sandbox / "renamed.txt"), "dst": str(sandbox / "sub")}, fctx)
        check("file_op copy/rename/mkdir/move", all(x.startswith("ok:") for x in (r, r2, r3, r4)) and (sandbox / "sub" / "renamed.txt").exists(),
              " | ".join(x[:50] for x in (r, r2, r3, r4)))
        r = await call("file_op", {"op": "zip", "src": str(sandbox / "sub")}, fctx)
        r2 = await call("file_op", {"op": "unzip", "src": str(sandbox / "sub.zip"), "dst": str(sandbox / "unzipped")}, fctx)
        check("file_op zip/unzip", r.startswith("ok:") and r2.startswith("ok:") and (sandbox / "unzipped" / "sub" / "renamed.txt").exists(), f"{r[:60]} | {r2[:60]}")
        r = await call("file_op", {"op": "size", "src": str(sandbox)}, fctx)
        r2 = await call("file_op", {"op": "info", "src": str(f)}, fctx)
        check("file_op size/info", r.startswith("ok:") and r2.startswith("ok:"), f"{r[:80]} | {r2[:80]}")
        for i, days in enumerate((1, 5, 3)):
            p = sandbox / "newest" / f"f{i}.txt"
            p.parent.mkdir(exist_ok=True)
            p.write_text("x")
            os.utime(p, (time.time() - days * 86400,) * 2)
        r = await call("list_files", {"folder": str(sandbox / "newest"), "n": 2}, fctx)
        check("list_files newest first", r.startswith("ok:") and r.index("f0.txt") < r.index("f2.txt") and "f1.txt" not in r, r[:200])
        (sandbox / "a" / "b").mkdir(parents=True, exist_ok=True)
        (sandbox / "a" / "b" / "quarterly-report-x7.txt").write_text("q")
        t0 = time.time()
        r = await call("find_file", {"name": "quarterly-report-x7.txt", "where": str(sandbox)}, fctx)
        check("find_file (walk)", r.startswith("ok:") and "quarterly-report-x7.txt" in r, f"{time.time() - t0:.2f}s {r[:150]}")
        r = await call("wait_until", {"cond": "file_exists", "target": str(f), "timeout": 2}, fctx)
        r2 = await call("wait_until", {"cond": "file_exists", "target": str(sandbox / "never.txt"), "timeout": 0.6}, fctx)
        check("wait_until file_exists / timeout", r.startswith("ok:") and r2.startswith("error:TIMEOUT"), f"{r[:60]} | {r2[:80]}")
        r = await call("screenshot", {"display": 0, "path": str(sandbox / "shot.png")}, fctx)
        check("screenshot", r.startswith("ok:") and (sandbox / "shot.png").exists(), r[:120])
        doc = sandbox / "doc.docx"
        with zipfile.ZipFile(doc, "w") as z:
            z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>Hello docx world</w:t></w:r></w:p></w:body></w:document>")
        r = await call("read_file", {"path": str(doc)}, fctx)
        check("read_file docx", "Hello docx world" in r, r[:120])
        if recycle:
            r = await call("file_op", {"op": "recycle", "src": str(sandbox / "unzipped")}, fctx)
            check("file_op recycle", r.startswith("ok:") and not (sandbox / "unzipped").exists(), r[:120])
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    check("sandbox removed", not sandbox.exists(), str(sandbox))

    # --- guards ---
    claude = next((w for w in windows() if w.exe == "claude.exe"), None)
    if claude:
        r = await call("read_window", {"window": f"hwnd:{claude.hwnd}"}, ctx)
        r2 = await call("click", {"target": "Send", "window": f"hwnd:{claude.hwnd}"}, ctx)
        check("protected window guard", r.startswith("error:BLOCKED") and r2.startswith("error:BLOCKED"), f"{r[:70]} | {r2[:70]}")
    chrome = next((w for w in windows() if w.exe == "chrome.exe"), None)
    if chrome:
        r = await call("type_into", {"field": "Address and search bar", "text": "x", "window": f"hwnd:{chrome.hwnd}"}, ctx)
        check("browser window guard", r.startswith("error:BLOCKED"), r[:120])
    r = await call("type_into", {"field": "Password", "text": "hunter2"}, ctx)
    r2 = await call("type_into", {"field": "Card", "text": "4111 1111 1111 1111"}, ctx)
    check("credential guard", r.startswith("error:BLOCKED") and r2.startswith("error:BLOCKED"), f"{r[:60]} | {r2[:60]}")
    r = await call("hotkeys", {"keys": ["win+r"], "window": "Notepad"}, ctx)
    check("hotkeys win+r refused", r.startswith("error:BLOCKED") or r.startswith("error:NOT_FOUND"), r[:100])
    r = await call("web_search", {"query": "x"}, ctx)
    check("web without a browser -> NEEDS", r.startswith("error:NEEDS"), r[:100])
    r = await call("game_state", {}, ctx)
    check("game without a loop -> NEEDS", r.startswith("error:NEEDS"), r[:100])

    if desktop:
        await _selftest_notepad(check, vision)
        await _selftest_calculator(check)
    return results


async def _tab_names(w: W) -> list[str]:
    found = await on_uia(_controls_sync, w.hwnd, {"TabItem"}, "", timeout=4.0)
    return [nm for _k, nm, *_r in found]


async def _close_our_tabs(ctx: Ctx, w: W, theirs: list[str]) -> None:
    """Win11 Notepad keeps every tab for its next start, so the selftest closes the tabs it made (Ctrl+W, Don't save)
    and never touches tabs that were restored from the user's own session."""
    for _ in range(6):
        if not _alive(w.hwnd):
            return
        ours = [t for t in await _tab_names(w) if t not in theirs]
        if not ours:
            break
        await call("click", {"target": ours[0], "window": f"hwnd:{w.hwnd}", "how": "uia"}, ctx)
        if not await focus(w):
            break  # never Ctrl+W into whatever else is in front (the user's browser tab)
        await clear_tips(w, strict=False)
        try:
            press(w, "ctrl+w")
        except Fail:
            break
        await asyncio.sleep(0.8)
        if _alive(w.hwnd):
            await on_uia(_press_button_sync, w.hwnd, SAVE_BUTTONS["discard"], timeout=4.0)
            await asyncio.sleep(0.5)
    if _alive(w.hwnd):
        _u.PostMessageW(w.hwnd, 0x0010, 0, 0)  # the user's restored tabs stay in Notepad's session
        await asyncio.sleep(1.0)


async def _selftest_notepad(check, vision: bool) -> None:
    if any(w.exe == "notepad.exe" for w in windows()):
        check("notepad round trip", True, "skipped: the user has Notepad open")
        return
    sandbox = Path(os.environ["TEMP"]) / f"io-actions-notepad-{os.getpid()}"
    sandbox.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(options={}, request=f"selftest: notepad, save to {sandbox}")
    t_start = time.time()
    w = None
    theirs: list = []
    try:
        r = await call("open_app", {"name": "Notepad", "new": True}, ctx)
        check("open_app Notepad", r.startswith("ok:"), r[:150])
        w = next((x for x in windows() if x.exe == "notepad.exe" and x.hwnd in ctx.opened), None) or resolve(ctx, "Notepad")
        if w is None:
            check("notepad window", False, "no window")
            return
        win = f"hwnd:{w.hwnd}"
        await asyncio.sleep(1.0)  # Notepad restores its session and shows "New in Notepad" a moment after the window appears
        theirs = [t for t in await _tab_names(w) if not t.startswith("Untitled. Unmodified")]
        tip = await call("dismiss_dialog", {"window": win}, ctx) if (await on_uia(_tip_sync, w.hwnd, timeout=4.0)) else "no popup"
        check("dismiss in-window popup (not the window's Close)", _alive(w.hwnd), tip[:120])
        if theirs:  # the user's restored tabs: work in a new one
            await call("hotkeys", {"keys": ["ctrl+n"], "window": win}, ctx)
            await asyncio.sleep(0.8)
        t0 = time.time()
        r = await call("type_into", {"field": "document", "text": "hello from the IO selftest\nline two", "window": win}, ctx)
        check("type_into document", r.startswith("ok:"), f"{time.time() - t0:.2f}s {r[:150]}")
        r = await call("read_window", {"window": win, "find": "selftest"}, ctx)
        check("read_window find", r.startswith("ok:") and "hello from the IO selftest" in r, r[:150])
        r = await call("list_controls", {"window": win, "kind": "menu"}, ctx)
        check("list_controls menu", r.startswith("ok:") and "File" in r, r[:200])
        r = await call("find_control", {"window": win, "text": "File"}, ctx)
        check("find_control File", r.startswith("ok: yes"), r[:150])
        r = await call("click", {"target": "Nonexistent Button Zq", "window": win, "how": "uia"}, ctx)
        check("click NOT_FOUND names closest", r.startswith("error:NOT_FOUND"), r[:150])
        t0 = time.time()
        r = await call("select_menu", {"path": "File > Save as", "window": win}, ctx)
        dlg = next((x for x in windows(owned=True) if x.pid == w.pid and x.hwnd != w.hwnd and x.cls == "#32770"), None)
        check("select_menu File > Save as", r.startswith("ok:") and dlg is not None, f"{time.time() - t0:.2f}s {r[:150]}")
        if dlg:
            r = await call("dismiss_dialog", {"window": f"hwnd:{dlg.hwnd}", "choice": "cancel"}, ctx)
            await asyncio.sleep(0.4)
            check("dismiss_dialog cancel", r.startswith("ok:") and not _alive(dlg.hwnd), r[:150])
        target = sandbox / "saved.txt"
        t0 = time.time()
        r = await call("save_file_as", {"path": str(target), "window": win}, ctx)
        check("save_file_as", r.startswith("ok:") and target.exists() and "hello from the IO selftest" in target.read_text(encoding="utf-8", errors="replace"),
              f"{time.time() - t0:.2f}s {r[:160]}")
        r = await call("window_state", {"window": win, "state": "max"}, ctx)
        r2 = await call("window_state", {"window": win, "state": "restore"}, ctx)
        check("window_state max/restore", "maximised" in r and r2.startswith("ok:") and "normal" in r2, f"{r[:70]} | {r2[:70]}")
        r = await call("hotkeys", {"keys": ["ctrl+end", "text:\nmore text", "wait:0.2"], "window": win}, ctx)
        r2 = await call("wait_until", {"cond": "text_appears", "target": "more text", "window": win, "timeout": 3}, ctx)
        seen = "" if r2.startswith("ok:") else (await call("read_window", {"window": win, "max": 400}, ctx))[:200]
        check("hotkeys + wait_until text_appears", r.startswith("ok:") and r2.startswith("ok:"), f"{r[:70]} | {r2[:70]} {seen}")
        r = await call("focus_window", {"window": win}, ctx)
        check("focus_window", r.startswith("ok:"), r[:100])
        if vision:
            r = await call("check_screen", {"question": "Is a Notepad window with text visible?", "window": win}, ctx)
            check("check_screen (vision)", r.startswith("ok:") or r.startswith("unsure:"), r[:150])
        await _close_our_tabs(ctx, w, theirs)
        check("Notepad closed with no selftest tab kept", not _alive(w.hwnd), f"restored user tabs left alone: {len(theirs)}")
    finally:
        if w is not None and _alive(w.hwnd):
            await _close_our_tabs(ctx, w, theirs)
        shutil.rmtree(sandbox, ignore_errors=True)
        check("notepad round trip time", time.time() - t_start < 60, f"{time.time() - t_start:.1f}s")


async def _selftest_calculator(check) -> None:
    if any(w.exe == "calculatorapp.exe" or w.title == "Calculator" for w in windows()):
        check("calculator round trip", True, "skipped: Calculator is already open")
        return
    ctx = Ctx(options={}, request="selftest: calculator")
    t_start = time.time()
    w = None
    try:
        r = await call("open_app", {"name": "Calculator"}, ctx)
        check("open_app Calculator", r.startswith("ok:"), r[:150])
        w = next((x for x in windows() if x.hwnd in ctx.opened), None) or resolve(ctx, "Calculator")
        if w is None:
            return
        win = f"hwnd:{w.hwnd}"
        await asyncio.sleep(0.5)
        await call("hotkeys", {"keys": ["esc"], "window": win}, ctx)  # clear
        t0 = time.time()
        rs = [await call("click", {"target": t, "window": win}, ctx) for t in ("Five", "Plus", "Three", "Equals")]
        check("click Five + Three =", all(x.startswith("ok:") for x in rs), f"{time.time() - t0:.2f}s " + " | ".join(x[:40] for x in rs))
        r = await call("read_window", {"window": win, "find": "Display"}, ctx)
        check("read_window Calculator display", "Display is 8" in r, r[:150])
        r = await call("list_controls", {"window": win, "kind": "button", "filter": "Seven"}, ctx)
        r2 = await call("click", {"target": "#1", "window": win}, ctx) if r.startswith("ok:") else r
        r3 = await call("find_control", {"window": win, "text": "Display is"}, ctx)
        check("list_controls + click #1", r2.startswith("ok:") and "Display is 7" in r3, f"{r2[:60]} | {r3[:80]}")
        r = await call("click", {"target": "Equals", "window": win, "expect": "Display is 7"}, ctx)
        check("click with expect", r.startswith("ok:") or r.startswith("error:NO_CHANGE"), r[:120])
        r = await call("close_window", {"window": win}, ctx)
        check("close_window Calculator", r.startswith("ok:") and not _alive(w.hwnd), r[:120])
    finally:
        if w is not None and _alive(w.hwnd):
            _u.PostMessageW(w.hwnd, 0x0010, 0, 0)
        check("calculator round trip time", time.time() - t_start < 60, f"{time.time() - t_start:.1f}s")


async def _selftest_web(check) -> None:
    """WEB actions in a headless Edge with a throwaway profile: never the user's Chrome, never IO's own browser profile."""
    import tempfile
    from contextlib import AsyncExitStack
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    h = _h()
    prof = tempfile.mkdtemp(prefix="io-actions-web-")
    try:
        async with AsyncExitStack() as stack:
            params = StdioServerParameters(command="node", args=[str(h.BROWSER_CLI), "--headless", "--browser", "msedge", "--user-data-dir", prof,
                                                                  "--codegen", "none", "--image-responses", "omit"], cwd=str(h.HERE / "data" / "browser"))
            r, w = await stack.enter_async_context(stdio_client(params, errlog=open(os.devnull, "w")))
            s = await stack.enter_async_context(ClientSession(r, w, read_timeout_seconds=60))
            await s.initialize()
            ctx = Ctx(options={}, browser=s, browser_mode="edge", request="selftest: example.com, click More information")
            res = await call("read_page", {"url": "example.com"}, ctx)
            check("web read_page", res.startswith("ok:") and "Example Domain" in res, res[:120])
            res = await call("read_page", {"what": "links"}, ctx)
            check("web read_page links", res.startswith("ok:") and "iana" in res.lower(), res[:120])
            res = await call("web_click", {"text": "More information"}, ctx)
            if res.startswith("error:NOT_FOUND"):
                res = await call("web_click", {"text": "Learn more"}, ctx)
            check("web_click", res.startswith("ok:") and "iana" in res.lower(), res[:150])
            check("web_click refuses buy", (await call("web_click", {"text": "Buy now"}, ctx)).startswith("error:BLOCKED"), "BLOCKED")
            res = await call("web_search", {"query": "python 3.13 release date"}, ctx)
            check("web_search", res.startswith("ok:") or res.startswith("error:BLOCKED"), res[:150])
            res = await call("web_answer", {"question": "When was Python 3.13 released?"}, ctx)
            check("web_answer", res.startswith("ok:") or res.startswith("error:BLOCKED"), res[:150])
            await call("read_page", {"url": "https://www.wikipedia.org/"}, ctx)
            res = await call("web_fill", {"fields": {"Search Wikipedia": "Io moon"}, "submit": True}, ctx)
            check("web_fill + submit", res.startswith("ok:") and "wiki" in res.lower(), res[:150])
            res = await call("web_close_tab", {}, ctx)
            check("web_close_tab", res.startswith("ok:") and not ctx.tab_open, res)
    finally:
        shutil.rmtree(prof, ignore_errors=True)


async def _selftest_apps(check) -> None:
    """File Explorer on a temp folder, Settings > Display, write_in_app and the Save As dropdown: windows opened here
    and closed again (Settings only if the user doesn't have it open)."""
    sandbox = Path(os.environ["TEMP"]) / f"io-actions-apps-{os.getpid()}"
    folder = sandbox / "io-explorer-test"
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(30):
        (folder / f"file-{i:02d}.txt").write_text("x" * (i + 1))
    ctx = Ctx(options={}, request=f"selftest in {sandbox}; open the display settings; write in Notepad")
    before = {w.hwnd for w in windows()}
    try:
        res = await call("open_path", {"path": str(folder)}, ctx)
        w = next((x for x in windows() if x.hwnd not in before and x.exe == "explorer.exe"), None)
        check("open_path folder", res.startswith("ok:") and w is not None, res[:120])
        if w:
            await asyncio.sleep(1.0)
            res = await call("read_table", {"window": f"hwnd:{w.hwnd}", "max_rows": 5}, ctx)
            check("read_table Explorer", res.startswith("ok:") and "file-0" in res, res[:120])
            res = await call("scroll_until", {"target": "file-29.txt", "window": f"hwnd:{w.hwnd}"}, ctx)
            check("scroll_until in a virtual list", res.startswith("ok:"), res[:120])
            res = await call("close_window", {"window": f"hwnd:{w.hwnd}"}, ctx)
            check("close Explorer", res.startswith("ok:"), res[:80])
        if not any(x.title == "Settings" or x.exe == "systemsettings.exe" for x in windows()):
            res = await call("open_settings", {"page": "display"}, ctx)
            r2 = await call("read_window", {"window": "Settings", "find": "Scale"}, ctx)
            r3 = await call("close_window", {"window": "Settings"}, ctx)
            check("open_settings / read / close", res.startswith("ok:") and "Display" in r2 and r3.startswith("ok:"), f"{res[:60]} | {r3[:40]}")
        if not any(x.exe == "notepad.exe" for x in windows()):
            res = await call("write_in_app", {"app": "Notepad", "text": "selftest write_in_app"}, ctx)
            check("write_in_app Notepad", res.startswith("ok:"), res[:150])
            npw = next((x for x in windows() if x.exe == "notepad.exe"), None)
            if npw:
                await call("select_menu", {"path": "File > Save as", "window": f"hwnd:{npw.hwnd}"}, ctx)
                dlg = next((x for x in windows(owned=True) if x.pid == npw.pid and x.cls == "#32770"), None)
                if dlg:
                    res = await call("set_control", {"label": "Save as type", "value": "All files", "window": f"hwnd:{dlg.hwnd}"}, ctx)
                    check("set_control dropdown", res.startswith("ok:") and "all files" in res.lower(), res[:120])
                    await call("dismiss_dialog", {"window": f"hwnd:{dlg.hwnd}"}, ctx)
                state0 = clip_state()
                res = await call("hotkeys", {"keys": ["ctrl+end", "text:\n" + "y" * 350], "window": f"hwnd:{npw.hwnd}"}, ctx)
                txt = await call("read_window", {"window": f"hwnd:{npw.hwnd}", "max": 3000}, ctx)
                check("long text pasted, clipboard restored", "y" * 350 in txt and clip_state() == state0, f"clipboard {state0[0]}")
                await _close_our_tabs(ctx, npw, [])
    finally:
        for x in windows():
            if x.hwnd not in before and x.exe == "explorer.exe" and "io-explorer-test" in x.title:
                _u.PostMessageW(x.hwnd, 0x0010, 0, 0)
        await asyncio.sleep(0.5)
        shutil.rmtree(sandbox, ignore_errors=True)


async def _selftest_vision(check) -> None:
    """Two model calls on a Calculator opened here: the vision click rung (EvoCUA) and check_screen."""
    if any(w.exe == "calculatorapp.exe" or w.title == "Calculator" for w in windows()):
        check("vision", True, "skipped: Calculator is already open")
        return
    ctx = Ctx(options={}, request="selftest: calculator vision")
    await call("open_app", {"name": "Calculator"}, ctx)
    w = next((x for x in windows() if x.hwnd in ctx.opened), None)
    if w is None:
        return
    try:
        win = f"hwnd:{w.hwnd}"
        await call("hotkeys", {"keys": ["esc"], "window": win}, ctx)
        res = await call("click", {"target": "the 7 key on the number pad", "window": win, "how": "vision"}, ctx)
        disp = await call("find_control", {"window": win, "text": "Display is"}, ctx)
        check("click how=vision", "Display is 7" in disp, f"{res[:80]} | {disp[:40]}")
        res = await call("check_screen", {"question": "Does the calculator's display show the number 7?", "window": win}, ctx)
        check("check_screen", res.startswith("ok: yes") or res.startswith("ok: no"), res[:100])
    finally:
        await call("close_window", {"window": f"hwnd:{w.hwnd}"}, ctx)


def selftest(argv: list | None = None) -> int:
    """--no-desktop: no windows at all. --apps: Explorer, Settings, write_in_app. --web: headless Edge with a throwaway
    profile. --vision: two model calls on Calculator. --recycle: also test file_op recycle (one temp folder to the Bin)."""
    argv = argv if argv is not None else sys.argv[1:]
    if HOST is None:
        sys.path.insert(0, str(HERE))
        import boss  # noqa: PLC0415 - its DPI awareness and helpers; boss binds itself once wired
        bind(boss)

    async def run_all() -> list:
        res = await _selftest(desktop="--no-desktop" not in argv, vision=False, recycle="--recycle" in argv)

        def check(name: str, passed: bool, evidence: str) -> None:
            res.append((name, bool(passed), str(evidence)[:300]))
            print(f"{'PASS' if passed else 'FAIL'}  {name}: {str(evidence)[:200]}".encode("ascii", "replace").decode(), flush=True)

        for flag, fn in (("--apps", _selftest_apps), ("--web", _selftest_web), ("--vision", _selftest_vision)):
            if flag in argv:
                try:
                    await fn(check)
                except Exception as e:
                    check(f"{flag} ran", False, f"{type(e).__name__}: {e}")
        return res

    res = asyncio.run(run_all())
    failed = [r for r in res if not r[1]]
    print(f"\n{len(res) - len(failed)}/{len(res)} passed" + (": failed " + ", ".join(r[0] for r in failed) if failed else ""))
    out = os.environ.get("IO_SELFTEST_JSON")
    if out:
        Path(out).write_text(json.dumps([{"check": c, "passed": p, "evidence": e} for c, p, e in res], indent=1), encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    # run against the importable module, so the registry is the one boss will use
    sys.path.insert(0, str(HERE))
    import actions as _self  # noqa: PLC0415
    sys.exit(_self.selftest())
