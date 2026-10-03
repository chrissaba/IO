"""ui-tars-boss: a small tool-calling text model drives Windows through Windows-MCP.

UI-TARS is only used as "eyes": when the boss can't find something in the accessibility
tree (unnamed icons, canvases, images), it calls find_on_screen and UI-TARS returns
where to click.

Usage:  python boss.py "open notepad and type hello"
"""
import argparse
import asyncio
import base64
import ctypes
import ctypes.wintypes as wt
import hashlib
import io
import itertools
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from contextlib import AsyncExitStack
from types import SimpleNamespace
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError
from openai import BadRequestError, OpenAI
from PIL import ImageGrab

import planner
import plugins

# physical pixels everywhere, matching Windows-MCP's virtual-desktop coordinates
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

HERE = Path(__file__).parent
BOSS_URL = os.environ.get("BOSS_URL", "http://127.0.0.1:8090/v1")
BOSS_MODEL = os.environ.get("BOSS_MODEL", "boss")
EYES_URL = os.environ.get("EYES_URL", "http://127.0.0.1:8888/v1")
EYES_MODEL = os.environ.get("EYES_MODEL", "mradermacher/UI-TARS-1.5-7B-GGUF")
MCP_TOOLS = "App,Snapshot,Click,Type,Scroll,Shortcut,WaitFor,PowerShell,Clipboard,Process,FileSystem,Scrape"
# Playwright MCP drives web pages by their elements, in one of two places:
# "edge": a separate Edge window with its own profile
# "chrome": your own Chrome through Playwright's extension (chrome.debugger), in a tab group named IO, the way
#           Claude in Chrome works; signed in as you. The extension's token skips its approval prompt.
BROWSER_CLI = HERE / "mcp" / "node_modules" / "@playwright" / "mcp" / "cli.js"
BROWSER_ARGS = ["--browser", "msedge", "--user-data-dir", str(HERE / "data" / "browser-profile"), "--codegen", "none", "--image-responses", "omit"]
CHROME_ARGS = ["--extension", "--browser", "chrome", "--codegen", "none", "--image-responses", "omit"]
BROWSER_CLIENT = types.Implementation(name="IO", version="1.0")  # the extension names the tab group after this
BROWSER_WHERE = {
    "edge": "They work in IO's own Edge window.",
    "chrome": ("They work in IO's own tab in the user's Chrome, inside the tab group named IO, signed in to the user's accounts: "
               "never buy, post, send or change account settings unless the task says to."),
}
BROWSER_TOOLS = {
    "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_click", "browser_type", "browser_fill_form",
    "browser_press_key", "browser_select_option", "browser_hover", "browser_wait_for", "browser_tabs", "browser_handle_dialog",
    "browser_file_upload", "browser_close",
}
MAX_MEMORY = 50
# llama.cpp caps Qwen2.5-VL images at 4096 visual tokens of 28x28 px; resize to that ourselves
# so UI-TARS's pixel coordinates refer to the exact image we sent
EYES_MAX_PIXELS = 4096 * 28 * 28
KEEP_FULL_SNAPSHOTS = 2
# replanning: after this many failed calls in a row, or steps without finishing, at most MAX_REPLANS times
REPLAN_AFTER_ERRORS = 2
REPLAN_AFTER_STEPS = 10
MAX_REPLANS = 3
MAX_TOOL_TEXT = 14000
# re-reading the same thing with nothing in between is a loop; repeating an action (undo x3, PageDown, Next) is not
LOOP_PRONE = {"Snapshot", "browser_snapshot", "browser_read", "look_at_screen", "find_on_screen", "click_on", "Scrape", "browser_open", "browser_navigate"}
# conditional offers ("If you want, I'll check the weekend too") aren't promises to keep working
OFFER = re.compile(r"[^.!?\n]*\b(if you(?:'d)? (?:want|like|need|prefer)|let me know|would you like|want me to|shall i|should i)\b[^.!?\n]*[.!?]?", re.I)


def intent_text(t: str) -> str:
    return OFFER.sub("", (t or "").replace("\u2019", "'")).lower()


MORE_TO_DO = re.compile(r"\b(i will|i'll|let me|i am going to|i'm going to) (now )?(try|search|look|check|read|scroll|open)\b")
# greetings and thanks: answered directly, without a plan
SMALL_TALK = re.compile(r"^(hi|hello|hey|yo|hiya|howdy|sup|thanks|thank you|thx|ty|cheers|good (morning|afternoon|evening|night)|"
                        r"how are you|how's it going|what's up|who are you|what are you|what can you do|nice|cool|great|ok|okay)\b", re.I)
PLUGIN_CALL_TIMEOUT = 120  # seconds one plugin tool call may take

SYSTEM = """You are IO, the user's assistant on their Windows PC. You can chat, answer questions, and do things on the PC with the tools provided.

HOW TO DECIDE
- Chatting (hello, thanks, how are you, what can you do) or a question you can answer from general knowledge: reply in plain text, no tools.
- You are IO. "What is IO?", "who are you" and questions about yourself are about you: answer them yourself, no tools.
- Well-known facts (countries and capitals, famous people and companies, science, history): answer directly, no search.
- A name or word you don't confidently know (a small website, company, product, app, person, slang): search the web before
  answering, with browser_open on https://www.google.com/search?q=<the words> (always Google, never Bing). The result lists the top results with their
  addresses: answer from them if they already say what it is, otherwise browser_open the best result's address (don't click
  results). Don't guess a similar-sounding word, don't assume it means one of your tools, and don't ask the user what it is
  until you've searched. If it looks like a brand or website name, also try browser_open https://<name>.com.
- Needs live or personal information (the time, files, what's open or on screen, a web page, weather, prices): get it with a tool, then answer.
- Asks you to do something on the PC: do exactly that, nothing extra (no saving, closing or double-checking unless asked).
- Ambiguous, or needs something only the user knows (which file, which account): call ask_user instead of guessing.

PICK THE RIGHT TOOL (cheapest reliable one first)
- PowerShell runs a command and returns its output (never open a PowerShell or Terminal window for it). Use it for facts about the PC, files and time:
  time in a city: [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'Tokyo Standard Time').ToString('h:mm tt, dddd')
  newest files: Get-ChildItem "$env:USERPROFILE\\Downloads" -File | Sort-Object LastWriteTime -Descending | Select-Object -First 5 Name, LastWriteTime
  find files: Get-ChildItem "$env:USERPROFILE" -Recurse -File -Filter *report* -ErrorAction SilentlyContinue | Select-Object -First 20 FullName
  disk space: Get-PSDrive -PSProvider FileSystem | Select-Object Name, @{n='FreeGB';e={[math]::Round($_.Free/1GB,1)}}
  system: Get-CimInstance Win32_OperatingSystem, Get-Process | Sort-Object CPU -Descending | Select-Object -First 10, Get-NetIPAddress
  installed apps: Get-ItemProperty HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*, HKLM:\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*, HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\* | Where-Object DisplayName | Sort-Object DisplayName | Select-Object DisplayName
  installed Steam games: Get-ChildItem "C:\\Program Files (x86)\\Steam\\steamapps\\appmanifest_*.acf" | ForEach-Object { (Select-String -Path $_.FullName -Pattern '"name"\\s+"(.+?)"').Matches[0].Groups[1].Value } (more game folders are listed in steamapps\\libraryfolders.vdf)
  What's installed, an app's settings or saved data: read it like this (registry, the app's own files) rather than clicking through the app's window, which is slow and its window lists can be huge.
  Quote paths with spaces. If a command errors, read the error, fix the command and run it again.
- Snapshot lists open windows and on-screen controls as text with (x,y) coordinates. Use it to see what's open, to read text in a window, and before clicking.
- close_windows closes windows like their X button: by title, or everything except some (IO is never closed).
- browser_* tools only act on IO's own browser tab (opened with browser_open), never on desktop windows, dialogs such as
  "Save changes?", or the user's own Chrome tabs. For those use Snapshot, Click and find_on_screen.
- App launches or switches to an app by name. After launching, the app may open behind another window: switch to it before typing.
- Click/Type take loc=[x, y] from Snapshot (or from find_on_screen). type_text types into whatever has focus; Shortcut presses keys (ctrl+s, alt+tab, enter).
- find_on_screen finds something visually (icons, images, games) and returns x,y to Click. look_at_screen answers a question about what a monitor shows.
- FileSystem reads and writes files. For web pages prefer browser_open and browser_read; Scrape is a quick fallback that some sites (like Wikipedia) block with 403.
- If a tool fails, try another way before giving up (Scrape blocked: browser_open the page; one site down: another source).
- For anything on a website or in a browser, call browser_open first, then use the browser_* tools (click and type by the element refs in the page snapshot). For a fact on a long page, call browser_read with find set to a few words instead of re-reading snapshots. {browser_where} Never drive Chrome or Edge windows with App, Click, Type or Shortcut, and never launch a browser with App.
- remember saves a lasting fact the user will want reused (a path, a preference).

COMMON JOBS
- Write something in an app: App (launch), then type_text the text. Don't save unless asked.
- What's open: Snapshot, then list the window titles (skip system UI like the taskbar).
- What's on screen: look_at_screen, then put the full description in your answer.
- A fact on a web page: browser_open the most direct page (e.g. https://en.wikipedia.org/wiki/Topic), then browser_read with several words for the same thing (size: diameter radius dimensions; when: date founded born released). Work out the answer from what you find (a radius doubled is a diameter).
- A web page that is a list of different meanings (a disambiguation page, "may refer to", "most commonly refers to"): browser_open the link that matches (its address), then read that page. browser_click needs a ref from browser_snapshot, not link text.
- Games and emulators (BlueStacks): nothing is in Snapshot. Use find_on_screen and look_at_screen with window set to the app's title. Never press Esc or Back there (Esc is Android's Back and can close the game); close menus with their on-screen X.

RULES
- To read text in a window use Snapshot or look_at_screen, never select-all and copy (that replaces the user's clipboard).
- For questions about what is on screen, call look_at_screen. It sees the PC's monitors, not IO's browser tab: answer questions about a web page from browser_snapshot (its title, headings and text).
- If a tool fails, fix the call and try again or try another way; never report success for a step that failed.
- Finish with done. Its summary is your answer to the user: include the actual result (the time, the list, the description, the number), never just "I found it" or "I described it". If something blocked you, say what."""

EXTRA_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "Ask the user a question and wait for their answer. Use when the task is ambiguous or needs information only they have.",
            "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Save a short lasting note for future tasks (e.g. where a file lives, which account to use).",
            "parameters": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "look_at_screen",
            "description": "Look at a screenshot of a display and answer a question about it (what is shown, what an image or video contains, what text says). Use for questions about what is visible on the PC's monitors; for web pages opened with browser_open, use browser_snapshot instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "What you want to know about the screen"},
                    "window": {"type": "string", "description": "Optional: part of a window title (e.g. 'BlueStacks') to look only at that window"},
                    "display": {"type": "integer", "description": "Display index from Snapshot (0 = primary)", "default": 0},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_windows",
            "description": ("Close windows the way their X button does (apps can still ask to save; IO is never closed). Give titles "
                            "(parts of window titles, as listed by Snapshot), or all_except to close everything except those, e.g. "
                            "all_except: [] closes all app windows. Use this to close apps, never App or Process."),
            "parameters": {"type": "object", "properties": {
                "titles": {"type": "array", "items": {"type": "string"}, "description": "Parts of the titles of windows to close"},
                "all_except": {"type": "array", "items": {"type": "string"}, "description": "Close every app window except these"},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text into the control that currently has keyboard focus (e.g. a just-opened editor). Use Type with loc when you need to click a field first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "press_enter": {"type": "boolean", "default": False},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_on_screen",
            "description": "Visually locate something on a display when it is not in the Snapshot's control list. Returns screen coordinates {x, y} to use with Click.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "What to find, e.g. 'the red record button' or 'the gear icon in the top right of Spotify'"},
                    "window": {"type": "string", "description": "Part of the title of the window to search in (e.g. 'BlueStacks'). Strongly recommended: it keeps clicks inside that app"},
                    "display": {"type": "integer", "description": "Display index from Snapshot (0 = primary)", "default": 0},
                },
                "required": ["description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click_on",
            "description": "Find something on screen by how it looks and click it, in one step (find_on_screen + Click). Best for games, emulators and anything not in Snapshot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "What to click, e.g. 'the red Complete Task button'"},
                    "window": {"type": "string", "description": "Part of the title of the window it is in (e.g. 'BlueStacks'). Keeps the search inside that app"},
                    "display": {"type": "integer", "description": "Display index from Snapshot (0 = primary)", "default": 0},
                },
                "required": ["description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hold_on",
            "description": "Find something on screen by how it looks, then press and hold the mouse on it for some seconds (for 'hold to mine', 'hold to charge').",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "What to hold on, e.g. 'the grey tin rock nearest the character'"},
                    "seconds": {"type": "number", "description": "How long to hold (0.2 to 15)", "default": 2},
                    "window": {"type": "string", "description": "Part of the title of the window it is in (e.g. 'BlueStacks'). Keeps the search inside that app"},
                    "display": {"type": "integer", "description": "Display index from Snapshot (0 = primary)", "default": 0},
                },
                "required": ["description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Pause for a number of seconds you choose before the next action (an animation, a loading screen, a timer or resources building up in a game).",
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {"type": "number", "description": "How long to wait (0.5 to 600)"},
                    "reason": {"type": "string", "description": "What you are waiting for"},
                },
                "required": ["seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hold",
            "description": "Press and hold the left mouse button at a point for some seconds, then release. For games and controls that say 'hold' (hold to mine, hold to charge); Click only taps.",
            "parameters": {
                "type": "object",
                "properties": {
                    "loc": {"type": "array", "items": {"type": "integer"}, "description": "[x, y] screen coordinates, e.g. from find_on_screen"},
                    "seconds": {"type": "number", "description": "How long to hold (0.2 to 15)", "default": 2},
                },
                "required": ["loc"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Finish the task. Call as soon as the task is complete or cannot be completed.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string", "description": "What was done, or what blocked the task"}},
                "required": ["summary"],
            },
        },
    },
]

# UI-TARS-1.5's single-point grounding prompt; on taskbar icons it lands within a few pixels,
# where the multi-step agent prompt was off by hundreds
EYES_PROMPT = "Output only the coordinate of one point in your response. What element matches the following task: {target}"


STUDIO_KEY_FILE = HERE / "data" / "studio.json"


def studio_key() -> str:
    """Studio API key: STUDIO_API_KEY, else the one saved in UI-TARS Desktop's settings (copied into IO's data whenever
    it changes), else IO's copy (UI-TARS Desktop's folder may only exist inside another app's private storage)."""
    if os.environ.get("STUDIO_API_KEY"):
        return os.environ["STUDIO_API_KEY"]
    try:
        saved = json.loads(STUDIO_KEY_FILE.read_text(encoding="utf-8")).get("key", "")
    except (OSError, ValueError, AttributeError):
        saved = ""
    rel = Path("ui-tars-desktop") / "ui_tars.setting.json"
    candidates = [Path(os.environ["APPDATA"]) / rel]
    candidates += sorted(Path(os.environ["LOCALAPPDATA"], "Packages").glob(f"*/LocalCache/Roaming/{rel.as_posix()}"))
    for settings in candidates:
        try:
            key = json.loads(settings.read_text(encoding="utf-8")).get("vlmApiKey", "")
        except (OSError, ValueError):
            continue
        if key:
            if key != saved:
                STUDIO_KEY_FILE.parent.mkdir(exist_ok=True)
                STUDIO_KEY_FILE.write_text(json.dumps({"key": key}), encoding="utf-8")
            return key
    return saved


def displays() -> list[tuple[int, int, int, int]]:
    """Monitor rects (left, top, right, bottom) in physical pixels, primary first."""
    rects: list[tuple[bool, tuple[int, int, int, int]]] = []

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

    def cb(hmon, _hdc, _rect, _data):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(mi)
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        r = mi.rcMonitor
        rects.append((bool(mi.dwFlags & 1), (r.left, r.top, r.right, r.bottom)))
        return True

    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)
    ctypes.windll.user32.EnumDisplayMonitors(None, None, proc(cb), 0)
    rects.sort(key=lambda r: (not r[0], r[1][0]))
    return [r for _, r in rects]


def smart_size(w: int, h: int, factor: int = 28, max_pixels: int = EYES_MAX_PIXELS) -> tuple[int, int]:
    """Largest factor-aligned size with the same aspect ratio that fits max_pixels."""
    scale = min(1.0, math.sqrt(max_pixels / (w * h)))
    return max(factor, int(w * scale // factor) * factor), max(factor, int(h * scale // factor) * factor)


def find_window(title: str) -> tuple[int, tuple[int, int, int, int]] | None:
    """The visible top-level window whose title contains `title` (case-insensitive): (hwnd, rect)."""
    user32 = ctypes.windll.user32
    found: list = []

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if title.lower() in buf.value.lower():
                    r = wt.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(r))
                    if r.right - r.left > 50 and r.bottom - r.top > 50:
                        found.append((hwnd, (r.left, r.top, r.right, r.bottom)))
        return True

    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    user32.EnumWindows(proc(cb), 0)
    return found[0] if found else None


def content_rect(title: str) -> tuple[int, int, int, int] | None:
    """The part of a window that holds its actual content. Emulators and players draw it in one big child window
    (BlueStacks' game surface sits beside its ad and tool bars), so the largest child that fills a good share of the
    window is used; otherwise the whole window."""
    hit = find_window(title)
    if not hit:
        return None
    hwnd, (l, t, r, b) = hit
    user32 = ctypes.windll.user32
    best: list = []

    def cb(child, _):
        if user32.IsWindowVisible(child):
            rc = wt.RECT()
            user32.GetWindowRect(child, ctypes.byref(rc))
            area = max(0, rc.right - rc.left) * max(0, rc.bottom - rc.top)
            if not best or area > best[0]:
                best[:] = [area, (rc.left, rc.top, rc.right, rc.bottom)]
        return True

    user32.EnumChildWindows(hwnd, ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)(cb), 0)
    whole = (r - l) * (b - t)
    if best and 0.3 * whole <= best[0] < 0.97 * whole:
        return best[1]
    return (l, t, r, b)


def window_in(text: str) -> str:
    """The open window a request is about, by a word of its title (e.g. 'BlueStacks' in 'play the game in BlueStacks')."""
    low = text.lower()
    for _, title in open_windows():
        for word in re.findall(r"[A-Za-z][\w.-]{3,}", title):
            if word.lower() not in ("window", "player", "app", "windows", "microsoft") and re.search(r"\b" + re.escape(word.lower()) + r"\b", low):
                return word
    return ""


def open_windows() -> list[tuple[int, str]]:
    """Visible top-level app windows (hwnd, title), skipping system UI and IO itself."""
    user32 = ctypes.windll.user32
    out: list = []
    skip = {"IO", "Program Manager", "Settings", "Windows Input Experience", "Taskbar", "NVIDIA GeForce Overlay"}
    needed = ("unsloth", "llama-server")  # IO's own models run there: never close them

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):  # GW_OWNER: skip owned popups
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value not in skip and not any(k in buf.value.lower() for k in needed):
                    out.append((hwnd, buf.value))
        return True

    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    user32.EnumWindows(proc(cb), 0)
    return out


def close_windows(titles: list[str], all_but: list[str] | None = None) -> str:
    """Closes windows the polite way (like clicking X), so apps can still ask to save. IO itself is never closed."""
    user32 = ctypes.windll.user32
    wins = open_windows()
    if all_but is not None:
        keep = [k.lower() for k in all_but if k]
        targets = [(h, t) for h, t in wins if not any(k in t.lower() for k in keep)]
    else:
        wanted = [t.lower() for t in titles if t]
        targets = [(h, t) for h, t in wins if any(w in t.lower() for w in wanted)]
    for hwnd, _ in targets:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    time.sleep(1.0)
    still = [t for h, t in targets if user32.IsWindow(h) and user32.IsWindowVisible(h)]
    closed = [t for h, t in targets if t not in still]
    msg = f"Closed {len(closed)} window(s): {', '.join(closed) or 'none'}."
    if still:
        msg += f" Still open (it may be asking to save): {', '.join(still)}."
    if not targets:
        msg = "No matching windows were open."
    return msg


def hold_mouse(x: int, y: int, seconds: float) -> str:
    """Left button down at (x, y), held, then up: what a long press in a game or emulator needs."""
    user32 = ctypes.windll.user32
    user32.SetProcessDPIAware()
    seconds = max(0.2, min(15.0, float(seconds or 2)))
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # left down
    try:
        time.sleep(seconds)
    finally:
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # left up, even if the task is stopped mid-hold
    return f"held the mouse at ({int(x)}, {int(y)}) for {seconds:g}s"


def focus_window(title: str) -> str:
    """Brings the window whose title contains `title` to the front (fallback when App switch fails)."""
    hit = find_window(title)
    if not hit:
        return ""
    user32 = ctypes.windll.user32
    user32.ShowWindow(hit[0], 9)  # SW_RESTORE
    # Windows only lets the foreground app hand over focus; a no-op Alt press satisfies that rule
    user32.keybd_event(0x12, 0, 0, 0)
    user32.SetForegroundWindow(hit[0])
    user32.keybd_event(0x12, 0, 2, 0)
    n = user32.GetWindowTextLengthW(hit[0])
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hit[0], buf, n + 1)
    return buf.value


def capture_area(display: int, window: str, content: bool = False) -> tuple[tuple[int, int, int, int], str]:
    """Screen rect to look at: the named window if given, else the display. Returns (rect, error)."""
    if window:
        # screenshots show whatever is on top, and clicks land there too: bring the window up first
        if not focus_window(window):
            return (0, 0, 0, 0), f"no visible window with '{window}' in its title"
        time.sleep(0.4)
        if content and (rect := content_rect(window)):
            return rect, ""
        hit = find_window(window)
        return hit[1], ""
    rects = displays()
    if not 0 <= display < len(rects):
        return (0, 0, 0, 0), f"display {display} does not exist; there are {len(rects)}"
    return rects[display], ""


NO_THINKING = {"chat_template_kwargs": {"enable_thinking": False}}  # llama-server: skip the reasoning for this request
QWEN_POINT_PROMPT = ("Find this on the screenshot: {target}\nAnswer only with JSON like {{\"point_2d\": [x, y]}}, the centre "
                     "of it, with x and y on a 0-1000 scale across the image's width and height.")


class Eyes:
    """Where to click and what's on screen. Fast mode: UI-TARS in Unsloth Studio finds things, the boss describes.
    Smart mode: the boss (Qwen) does both with its own vision."""

    def __init__(self, mode: str = "fast") -> None:
        self.mode = "smart" if mode in ("smart", "balanced") else "fast"  # the Qwen modes see for themselves
        if self.mode == "smart":
            self.client, self.model = OpenAI(base_url=BOSS_URL, api_key="local", max_retries=2, timeout=180), BOSS_MODEL
        else:
            self.client, self.model = OpenAI(base_url=EYES_URL, api_key=studio_key() or "local", max_retries=3, timeout=120), EYES_MODEL
        self.content = False  # loops: look only at the window's content area (a game without its emulator's side bars)
        self.brief = False  # loops: short looks, since it looks again before every move

    def find(self, description: str, display: int = 0, window: str = "") -> dict:
        (left, top, right, bottom), err = capture_area(display, window, self.content)
        if err:
            return {"error": err}
        shot = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
        iw, ih = smart_size(shot.width, shot.height)
        buf = io.BytesIO()
        shot.resize((iw, ih)).save(buf, format="PNG")
        url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

        prompt = QWEN_POINT_PROMPT if self.mode == "smart" else EYES_PROMPT
        reply = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            max_tokens=400 if self.mode == "smart" else 60,
            extra_body=NO_THINKING if self.mode == "smart" else None,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": url}},
                        {"type": "text", "text": prompt.format(target=description)},
                    ],
                }
            ],
        ).choices[0].message.content or ""
        reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.S)
        if self.mode == "smart":
            m = re.search(r"\[\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\]", reply)
            if not m:
                return {"error": "could not locate it", "eyes_said": reply[-300:]}
            px, py = float(m.group(1)), float(m.group(2))
            if not (0 <= px <= 1000 and 0 <= py <= 1000):  # off the image: a guess, not a sighting
                return {"error": f"could not locate it (the answer pointed off the {'window' if window else 'screen'})"}
            x = left + px / 1000 * (right - left)
            y = top + py / 1000 * (bottom - top)
            return {"x": round(x), "y": round(y)}
        m = re.search(r"\((\d+(?:\.\d+)?),\s*(\d+(?:\.\d+)?)\)", reply)
        if not m:
            return {"error": "could not locate it", "eyes_said": reply[-300:]}
        x = left + float(m.group(1)) * (right - left) / iw
        y = top + float(m.group(2)) * (bottom - top) / ih
        return {"x": round(x), "y": round(y)}

    def describe(self, question: str, display: int = 0, window: str = "") -> str:
        """Answers a question about a display's (or one window's) contents with the boss model's own vision."""
        rect, err = capture_area(display, window, self.content)
        if err:
            return err
        shot = ImageGrab.grab(bbox=rect, all_screens=True)
        shot.thumbnail((1920, 1920))
        buf = io.BytesIO()
        shot.convert("RGB").save(buf, format="JPEG", quality=85)
        url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        if self.brief:
            question = (question or "What is on the screen?") + " Answer in at most 3 short sentences: what screen or menu is open, and what can be done next."
        reply = OpenAI(base_url=BOSS_URL, api_key="local", max_retries=2, timeout=120).chat.completions.create(
            model=BOSS_MODEL,
            temperature=0.2,
            max_tokens=160 if self.brief else 700,
            extra_body=NO_THINKING if self.brief else None,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": url}},
                        {"type": "text", "text": f"This is a screenshot of {('the ' + window + ' window') if window else f'display {display}'}. {question or 'Describe what is on the screen.'}"},
                    ],
                }
            ],
        )
        return reply.choices[0].message.content or "(no answer)"


# Windows-MCP's label/labels args are ids from its annotated screenshot, which the text-only boss
# never sees; hiding them stops the boss passing control names there
HIDDEN_ARGS = {"label", "labels"}


def browser_params(options: dict) -> StdioServerParameters:
    browser_dir = HERE / "data" / "browser"
    browser_dir.mkdir(parents=True, exist_ok=True)
    if options.get("browser_mode") == "chrome":
        env = {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": options["chrome_token"]} if options.get("chrome_token") else None
        return StdioServerParameters(command="node", args=[str(BROWSER_CLI), *CHROME_ARGS], cwd=str(browser_dir), env=env)
    return StdioServerParameters(command="node", args=[str(BROWSER_CLI), *BROWSER_ARGS], cwd=str(browser_dir))


async def test_browser(options: dict, hold: float = 12) -> str:
    """Starts the browser tools the way a task would. In Chrome mode this connects to IO's extension, opens a
    "connected" page in the IO tab group and stays connected a few seconds so you can see the group."""
    async with AsyncExitStack() as stack:
        r, w = await stack.enter_async_context(stdio_client(browser_params(options), errlog=sys.stderr))
        session = await stack.enter_async_context(ClientSession(r, w, read_timeout_seconds=60, client_info=BROWSER_CLIENT))
        await session.initialize()
        if options.get("browser_mode") == "chrome":
            res = text_of(await session.call_tool("browser_navigate", {"url": "http://127.0.0.1:8765/static/connected.html"}))
            if res.startswith("error"):
                return res
            await asyncio.sleep(hold)
        return text_of(await session.call_tool("browser_tabs", {"action": "list"}))


BROWSER_READ_TOOL = {
    "type": "function",
    "function": {
        "name": "browser_read",
        "description": ("Read the text of the page open in IO's browser tab. Set find to a few words (e.g. 'diameter radius') to get "
                        "only the lines that mention them, which is best for facts on long pages. Without find it returns the start of the page."),
        "parameters": {"type": "object", "properties": {"find": {"type": "string", "description": "Words to look for"}}},
    },
}


def page_text(raw: str) -> str:
    """The page text out of a browser_evaluate result ('### Result' followed by a JSON string)."""
    m = re.search(r'### Result\s*\n(".*?")\s*(?:\n###|$)', raw, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except ValueError:
            pass
    return raw


FIND_STOPWORDS = {"the", "and", "for", "how", "what", "with", "from", "this", "that", "are", "was", "were", "its", "his", "her",
                  "who", "when", "where", "which", "does", "did", "has", "have", "much", "many", "about", "into", "tell", "find", "page"}


FIND_SYNONYMS = {
    "diameter": ["radius", "dimensions"], "size": ["diameter", "radius", "dimensions", "area"], "radius": ["diameter"],
    "height": ["tall", "elevation"], "tall": ["height", "elevation"], "weight": ["mass"], "mass": ["weight"],
    "age": ["born", "founded", "established"], "founded": ["established", "formed"], "born": ["birth"],
    "population": ["inhabitants", "residents"], "price": ["cost", "$"], "cost": ["price"], "distance": ["semi", "orbit", "km"],
}


def find_in_text(text: str, find: str, budget: int = 4000) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    raw = [w for w in re.findall(r"\w+", find.lower()) if len(w) > 2]
    words = [w for w in raw if w not in FIND_STOPWORDS] or raw
    words += [x for w in list(words) for x in FIND_SYNONYMS.get(w, []) if x not in words]  # pages often say it another way
    if not words:
        return text[:12000] + ("\n[page continues; use find to look for something specific]" if len(text) > 12000 else "")
    pats = [re.compile(r"\b" + re.escape(w)) for w in words]  # at word starts: 'age' doesn't hit 'page'
    score = {i: sum(1 for pt in pats if pt.search(l.lower())) for i, l in enumerate(lines)}
    hits = [i for i in range(len(lines)) if score[i]]
    if not hits:
        return f"No lines mention {find!r}. The page starts:\n" + "\n".join(lines)[:1500]
    hit_set = set(hits)

    def clip(i: int) -> str:  # a window around the match, so one long line can't use up the budget
        line, low = lines[i], lines[i].lower()
        pos = min((m.start() for pt in pats if (m := pt.search(low))), default=-1)
        if pos >= 0:
            a, b = max(0, pos - 150), pos + 250
        elif i - 1 in hit_set:  # the line after a hit: its start
            a, b = 0, 200
        else:  # the line before a hit: its end
            a, b = max(0, len(line) - 200), len(line)
        return ("…" if a > 0 else "") + line[a:b] + ("…" if b < len(line) else "")

    keep, used = set(), 0
    for i in sorted(hits, key=lambda i: -score[i]):  # best matches first, each with its neighbours
        window = [j for j in range(max(0, i - 1), min(len(lines), i + 2)) if j not in keep]
        cost = sum(len(clip(j)) for j in window)
        if keep and used + cost > budget:
            break
        keep.update(window)
        used += cost
    out, last = [], -2
    for i in sorted(keep):  # shown in page order
        out.append(("…\n" if i != last + 1 and out else "") + clip(i))
        last = i
    if len(keep) < len(hits):
        out.append("[more matches cut]")
    tip = "" if len(hits) > 3 else "\n[few matches: if this doesn't answer it, search again with other words for the same thing, e.g. radius or dimensions for size]"
    return "\n".join(out) + tip


DISAMBIGUATION = re.compile(r"\b(may|can|most commonly|commonly|usually|often) (also )?refers? to\b|\(disambiguation\)|topics referred to by the same term", re.I)
LINKS_JS = ("() => [...(document.querySelector('#mw-content-text, article, main') || document.body).querySelectorAll('a')]"
            ".map(a => [a.innerText.trim(), a.href]).filter(([t, h]) => t && h.startsWith(location.origin) && !h.includes('#')).slice(0, 300)")


RESULTS_JS = r"""() => [...document.querySelectorAll('#search a:has(h3), #rso a:has(h3)')].filter(a => a.href.startsWith('http')).slice(0, 8).map(a => {
  const box = a.closest('div[data-hveid], div.g') || a.parentElement;
  const text = (box && box.innerText || '').replace(/\s+/g, ' ');
  return [a.querySelector('h3').innerText.trim(), a.href, text.slice(0, 300)];
})"""


def _result_rows(raw: str):
    m = re.search(r"### Result\s*\n(.*?)(?:\n###|$)", raw, re.S)
    return json.loads(m.group(1) if m else raw)


async def search_results(session) -> str:
    """A Google results page as a short list of results with their real addresses, so the agent can answer from them or open one with browser_open."""
    try:
        rows = _result_rows(text_of(await session.call_tool("browser_evaluate", {"function": RESULTS_JS})))
    except Exception:
        return ""
    out = []
    for i, (title, url, snippet) in enumerate(rows, 1):
        out.append(f"{i}. {title or '(no title)'}\n   {url}\n   {snippet}")
    if not out:
        return ""
    return ("Search results (answer from these if they already say enough, otherwise browser_open the best one's address):\n"
            + "\n".join(out))


RESEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "research",
        "description": ("Look something up on the web without leaving the app you're using: searches Google in a hidden browser, reads "
                        "the top results and returns short practical notes (for example how a game mechanic works or what to do next)."),
        "parameters": {"type": "object", "properties": {"question": {"type": "string", "description": "What you want to know, as a search query"}},
                       "required": ["question"]},
    },
}
ASK_GEMINI_TOOL = {
    "type": "function",
    "function": {
        "name": "ask_gemini",
        "description": "Ask a much stronger model for advice when you're stuck after research; it also sees the window you're working in. Send one specific question.",
        "parameters": {"type": "object", "properties": {"question": {"type": "string", "description": "What you're stuck on, specifically"}},
                       "required": ["question"]},
    },
}
RESEARCH_SYSTEM = """You turn web search results and pages into practical notes for an assistant that is doing a task on a PC.
Answer the question for that task in at most 150 words: concrete steps, the names of buttons, menus and items, what to
do first and what to avoid. Only use what the sources say. Output only the notes."""


class Researcher:
    """Google in a headless browser of its own, so looking things up never steals focus from the app being driven."""

    def __init__(self, stack: AsyncExitStack) -> None:
        self.stack, self.session = stack, None

    async def _session(self):
        if self.session is None:
            params = StdioServerParameters(command="node", args=[str(BROWSER_CLI), "--headless", "--browser", "msedge", "--user-data-dir",
                                                                 str(HERE / "data" / "research-profile"), "--output-dir", str(HERE / "data" / "research"),
                                                                 "--codegen", "none", "--image-responses", "omit"])
            r, w = await self.stack.enter_async_context(stdio_client(params, errlog=sys.stderr))
            self.session = await self.stack.enter_async_context(ClientSession(r, w))
            await self.session.initialize()
        return self.session

    async def ask(self, question: str, task: str) -> str:
        s = await self._session()
        await s.call_tool("browser_navigate", {"url": "https://www.google.com/search?q=" + urllib.parse.quote_plus(question)})
        rows = _result_rows(text_of(await s.call_tool("browser_evaluate", {"function": RESULTS_JS})))
        if not rows:
            return "error: the search found nothing"
        sources = ["Search results:\n" + "\n".join(f"- {t}: {snip}" for t, _, snip in rows[:6])]
        for title, url, _ in rows[:2]:  # the top two pages, read as text
            try:
                await s.call_tool("browser_navigate", {"url": url})
                text = page_text(text_of(await s.call_tool("browser_evaluate", {"function": "() => document.body.innerText"})))
                sources.append(f"Page '{title}':\n{find_in_text(text, question, budget=3500)}")
            except Exception as e:
                log("warning", text=f"research couldn't read {url[:80]}: {e}")
        notes = await asyncio.to_thread(local_chat, RESEARCH_SYSTEM, f"Task: {task}\nQuestion: {question}\n\n" + "\n\n".join(sources)[:12000], 450, False)
        return notes or "error: couldn't make notes from the results"


GEMINI_URL = "https://gemini.google.com/app"
GEMINI_EVERY = 0  # seconds between questions; no limit: each answer takes 20-90s anyway
ADVISOR_MAX_CHARS = {"DuckAI": 4400, "Gemini": 8000}  # Duck.ai refuses messages over 4500 characters


def fit_advisor_prompt(limit: int, **parts: str) -> str:
    """GEMINI_PROMPT filled in and kept under the site's length limit: the oldest guide notes, actions and screen
    description are cut first; the goal and question always go whole."""
    budgets = {"guide": 1800, "screen": 1200, "actions": 900}
    for _ in range(12):
        prompt = GEMINI_PROMPT.format(**{k: (v[-budgets[k]:] if k in ("guide", "actions") else v[:budgets[k]]) if k in budgets else v
                                          for k, v in parts.items()})
        if len(prompt) <= limit:
            return prompt
        for k in budgets:
            budgets[k] = int(budgets[k] * 0.75)
    return prompt[:limit]
GEMINI_SEND_JS = """() => {
  const ed = document.querySelector('rich-textarea .ql-editor, div.ql-editor[contenteditable]');
  if (!ed) return 'no-editor';
  ed.focus();
  document.execCommand('insertText', false, %s);
  return 'ok';
}"""
# a screenshot goes in the way a person pastes one: a paste event carrying the image file
GEMINI_PASTE_JS = """() => {
  const ed = document.querySelector('rich-textarea .ql-editor, div.ql-editor[contenteditable]');
  if (!ed) return 'no-editor';
  const b = atob(%s), a = new Uint8Array(b.length);
  for (let i = 0; i < b.length; i++) a[i] = b.charCodeAt(i);
  const dt = new DataTransfer();
  dt.items.add(new File([a], 'screen.jpg', {type: 'image/jpeg'}));
  ed.focus();
  ed.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
  return 'ok';
}"""
GEMINI_ATTACHED_JS = """() => String(document.querySelectorAll('uploader-file-preview, .file-preview-container img, img[src^="blob:"], img[src^="data:image"]').length)"""
GEMINI_CLICK_SEND_JS = """() => {
  const b = [...document.querySelectorAll('button')].find(b => /send/i.test(b.getAttribute('aria-label') || '') && !b.disabled);
  if (!b) return 'no-send';
  b.click();
  return 'ok';
}"""
GEMINI_READ_JS = """() => {
  const r = [...document.querySelectorAll('model-response message-content, message-content')];
  const busy = !![...document.querySelectorAll('button')].find(b => /stop/i.test(b.getAttribute('aria-label') || ''));
  return JSON.stringify({n: r.length, busy, text: r.length ? r[r.length - 1].innerText : ''});
}"""
GEMINI_PROMPT = """I'm an AI agent running on someone's Windows PC, working on this goal on my own: {goal}
I'm stuck and would like advice from a stronger model.

What I learned from guides:
{guide}

What the screen shows now:
{screen}

My recent actions:
{actions}

{shot}My question: {question}
Reply with at most 120 words: concrete next steps (which button, menu or item, in order), and what I may be misunderstanding."""


# Director mode: the stronger model is the loop's brain. Each round it sees the goal, the window, what IO learned and what
# its last actions did, and answers with the next actions as JSON, like an API call. The local model only carries them out
# (it still finds things on screen for click_on/hold_on), and decides for itself only when the director can't be reached.
DIRECTOR_PROMPT = """You are the director of IO, an AI agent that operates a Windows PC by itself. You decide IO's next actions; a smaller
local model carries them out exactly. It finds things on screen from your descriptions, so describe each target by what it
looks like and where it is (e.g. "the red Fire button at the bottom right").

Goal (it never ends; the user stops it): {goal}
{shot}
{screen}Recent actions and their results, oldest first:
{history}

Tools:
{catalog}

Reply with ONLY a JSON object, no other text:
{{"thoughts": "<one sentence: what you see and why these actions>", "actions": [{{"tool": "<tool name>", "args": {{...}}}}]}}
Give 1 to 8 actions to do next, in order. Use wait when the game needs time. After they run you'll get the results and a new
screenshot. If an action fails, the rest are skipped and you're asked again."""


def tool_catalog(tools: list[dict]) -> str:
    """One line per tool for the director: name, arguments and what it does."""
    lines = []
    for t in tools:
        f = t["function"]
        params = ", ".join(k for k in f.get("parameters", {}).get("properties", {}) if k not in ("window", "display"))  # IO fills those in
        desc = "record a short progress note (IO keeps going)" if f["name"] == "done" else (f.get("description") or "").split(". ")[0][:120]
        lines.append(f"- {f['name']}({params}): {desc}")
    return "\n".join(lines)


def fit_director_prompt(limit: int, **parts: str) -> str:
    """DIRECTOR_PROMPT under the site's length limit: older guide notes, history and the screen description go first."""
    budgets = {"guide": 1400, "history": 1600, "screen": 700}

    def cut(k, v):  # whole lines only: the newest history, the start of the guide notes
        lines, out, n = v.splitlines(), [], 0
        for line in (reversed(lines) if k == "history" else lines):
            if n + len(line) > budgets[k]:
                break
            out.append(line)
            n += len(line) + 1
        return "\n".join(reversed(out) if k == "history" else out) or v[:budgets[k]]

    for _ in range(12):
        filled = {k: cut(k, v) if k in budgets else v for k, v in parts.items()}
        prompt = DIRECTOR_PROMPT.format(**filled)
        if len(prompt) <= limit:
            return prompt
        for k in budgets:
            budgets[k] = int(budgets[k] * 0.75)
    return prompt[:limit]


def parse_director(text: str, allowed: set) -> tuple[str, list[tuple[str, dict]]]:
    """(thoughts, [(tool, args)]) from the director's reply; tools IO doesn't have are dropped."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return "", []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return "", []
    out = []
    for a in data.get("actions") or []:
        if isinstance(a, dict) and a.get("tool") in allowed:
            out.append((a["tool"], a.get("args") if isinstance(a.get("args"), dict) else {}))
    return str(data.get("thoughts") or "")[:400], out[:8]


class Gemini:
    """Opt-in: asks Gemini for advice, a fresh chat each time, at most once every GEMINI_EVERY seconds.
    private: a throwaway signed-out browser (hidden, in-memory profile, Gemini's default model) that is closed after
    each question, so nothing is saved anywhere. Otherwise: the user's own Chrome (IO's tab group) and account."""

    def __init__(self, stack: AsyncExitStack, token: str, private: bool = True) -> None:
        self.stack, self.token, self.session, self.last, self.private = stack, token, None, 0.0, private
        self.takes_images = not private  # signed-out Gemini takes no uploads
        self.max_chars = ADVISOR_MAX_CHARS["Gemini"]

    async def ask(self, prompt: str, image: bytes = b"") -> str:
        if not self.private:
            return await self._ask(prompt, image)
        if wait := self.ready_in():
            return f"error: Gemini was asked recently; it can be asked again in {wait}s. Keep going with what you have."
        async with AsyncExitStack() as throwaway:
            out_dir = HERE / "data" / "gemini"
            out_dir.mkdir(parents=True, exist_ok=True)
            params = StdioServerParameters(command="node", args=[str(BROWSER_CLI), "--headless", "--browser", "msedge", "--isolated",
                                                                 "--output-dir", str(out_dir), "--codegen", "none", "--image-responses", "omit"])
            r, w = await throwaway.enter_async_context(stdio_client(params, errlog=sys.stderr))
            self.session = await throwaway.enter_async_context(ClientSession(r, w))
            await self.session.initialize()
            try:
                return await self._ask(prompt, image)
            finally:
                self.session = None  # the browser and its in-memory profile go away with this block

    async def _session(self):
        if self.session is None:
            r, w = await self.stack.enter_async_context(stdio_client(browser_params({"browser_mode": "chrome", "chrome_token": self.token}), errlog=sys.stderr))
            self.session = await self.stack.enter_async_context(ClientSession(r, w, client_info=BROWSER_CLIENT))
            await self.session.initialize()
        return self.session

    def ready_in(self) -> int:
        return max(0, round(self.last + GEMINI_EVERY - time.time()))

    async def _ask(self, prompt: str, image: bytes = b"") -> str:
        if wait := self.ready_in():
            return f"error: Gemini was asked recently; it can be asked again in {wait}s. Keep going with what you have."
        self.last = time.time()
        s = await self._session()
        await s.call_tool("browser_navigate", {"url": GEMINI_URL})
        await asyncio.sleep(2)
        if image:
            pasted = page_text(text_of(await s.call_tool("browser_evaluate", {"function": GEMINI_PASTE_JS % json.dumps(base64.b64encode(image).decode())})))
            if "no-editor" in pasted:
                return "error: Gemini's page isn't ready (sign in to gemini.google.com in Chrome)"
            for _ in range(15):  # wait for the upload to show before sending, or it goes without the picture
                await asyncio.sleep(1)
                n = page_text(text_of(await s.call_tool("browser_evaluate", {"function": GEMINI_ATTACHED_JS}))).strip().strip('"')
                if n.isdigit() and int(n) > 0:
                    break
            else:
                log("warning", text="the screenshot didn't attach in Gemini; asking without it")
                prompt = prompt.replace("A screenshot of the window I'm working in is attached.\n\n", "")
            await asyncio.sleep(1)
        sent = page_text(text_of(await s.call_tool("browser_evaluate", {"function": GEMINI_SEND_JS % json.dumps(prompt)})))
        if "no-editor" in sent:
            return "error: Gemini's page isn't ready (sign in to gemini.google.com in Chrome)"
        await asyncio.sleep(0.5)
        clicked = page_text(text_of(await s.call_tool("browser_evaluate", {"function": GEMINI_CLICK_SEND_JS})))
        if "no-send" in clicked:
            await s.call_tool("browser_press_key", {"key": "Enter"})
        text, same = "", 0
        for _ in range(60):  # up to about 2 minutes for Pro to think and answer
            await asyncio.sleep(2)
            try:
                state = json.loads(page_text(text_of(await s.call_tool("browser_evaluate", {"function": GEMINI_READ_JS}))))
            except ValueError:
                continue
            new = state.get("text", "").strip()
            same = same + 1 if new and new == text and not state.get("busy") else 0
            text = new
            if same >= 2:
                break
        text = re.sub(r"(?m)^\s*(JPG|JPEG|PNG|screen\.jpg)\s*$\n?", "", text).strip()  # the attachment's chips, not the answer
        return text[:2000] if text else "error: no answer from Gemini"


DUCK_URL = "https://duck.ai/"
DUCK_ATTACH_JS = """() => {
  const inp = document.querySelector('input[type=file]');
  if (!inp) return 'no-input';
  const b = atob(%s), a = new Uint8Array(b.length);
  for (let i = 0; i < b.length; i++) a[i] = b.charCodeAt(i);
  const dt = new DataTransfer();
  dt.items.add(new File([a], 'screen.jpg', {type: 'image/jpeg'}));
  inp.files = dt.files;
  inp.dispatchEvent(new Event('change', {bubbles: true}));
  return 'ok';
}"""
DUCK_TYPE_JS = """() => {
  const t = document.querySelector('textarea[name=user-prompt], textarea');
  if (!t) return 'no-textarea';
  Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(t, %s);
  t.dispatchEvent(new Event('input', {bubbles: true}));
  return 'ok';
}"""
DUCK_SEND_JS = """() => {
  const b = [...document.querySelectorAll('button')].find(b => (b.getAttribute('aria-label') || '') === 'Ask' || b.type === 'submit');
  if (!b || b.disabled) return 'no-send';
  b.click();
  return 'ok';
}"""
DUCK_READ_JS = """() => {
  const main = (document.querySelector('main') || document.body).innerText;
  const limited = /reached (your|the) (daily )?(usage |chat )?limit|limit (reached|exceeded)|try again (later|tomorrow)/i.test(main.slice(-1500));
  const challenge = [...document.querySelectorAll('[role=dialog], dialog')].some(d => d.offsetParent !== null && /challenge|human|bots use/i.test(d.innerText));
  const at = main.lastIndexOf('You said');
  const parts = at < 0 ? [] : main.slice(at).split('Duck.ai said');
  return JSON.stringify({limited, challenge, generating: /Generating response/.test(main), text: parts.length > 1 ? parts[parts.length - 1] : ''});
}"""
# Duck.ai keeps recent chats in the browser: delete just this one (its own Delete chat button), nothing else of yours
DUCK_NO_HISTORY_JS = """() => { localStorage.setItem('isRecentChatsOn', JSON.stringify('0')); return 'ok'; }"""  # Duck.ai's own "keep recent chats" switch
DUCK_FORGET_JS = """async () => {
  const del = [...document.querySelectorAll('button')].find(b => /^delete chat$/i.test(b.getAttribute('aria-label') || ''));
  if (!del) return 'no-delete';
  del.click();
  await new Promise(r => setTimeout(r, 800));
  const ok = [...document.querySelectorAll('[role=dialog] button, dialog button')].find(b => /delete/i.test(b.innerText || b.getAttribute('aria-label') || ''));
  if (ok) ok.click();
  return ok ? 'deleted' : 'clicked';
}"""


def duck_answer(text: str) -> str:
    """The reply out of the page text after 'Duck.ai said': without the model name above it or the app promo below."""
    text = re.split(r"\n(Duck\.ai works best|Jump to latest response|Download\n|Tools\n)", text)[0]
    lines = [l for l in text.strip().splitlines() if l.strip() and l.strip().lower() not in ("2nd opinion", "copy", "retry")]  # its buttons
    if lines and len(lines[0]) < 40 and not lines[0].rstrip().endswith((".", "!", "?")):
        lines = lines[1:]  # the model's name, e.g. "GPT-5.6 Luna"
    return "\n".join(lines).strip()


class DuckAI:
    """Opt-in: asks Duck.ai (DuckDuckGo's private chat, no account) in IO's tab in the user's Chrome, with a screenshot.
    A fresh chat each time, Duck.ai's local chat history wiped afterwards, at most once every GEMINI_EVERY seconds.
    If Duck.ai asks to prove you're human, IO stops: that's for you to do, not IO."""

    private, takes_images, max_chars = True, True, ADVISOR_MAX_CHARS["DuckAI"]

    def __init__(self, stack: AsyncExitStack, token: str) -> None:
        self.stack, self.token, self.session, self.last = stack, token, None, 0.0

    def ready_in(self) -> int:
        return max(0, round(self.last + GEMINI_EVERY - time.time()))

    async def _session(self):
        if self.session is None:
            r, w = await self.stack.enter_async_context(stdio_client(browser_params({"browser_mode": "chrome", "chrome_token": self.token}), errlog=sys.stderr))
            self.session = await self.stack.enter_async_context(ClientSession(r, w, client_info=BROWSER_CLIENT))
            await self.session.initialize()
        return self.session

    in_chat = False  # a director conversation is open in the tab

    async def ask(self, prompt: str, image: bytes = b"", keep: bool = False) -> str:
        """keep: continue the open conversation (and leave it open after) instead of a fresh, forgotten chat."""
        if wait := self.ready_in():
            return f"error: the advisor was asked recently; it can be asked again in {wait}s. Keep going with what you have."
        self.last = time.time()
        s = await self._session()

        async def js(code: str) -> str:
            return page_text(text_of(await s.call_tool("browser_evaluate", {"function": code})))

        if not (keep and self.in_chat):
            await s.call_tool("browser_navigate", {"url": DUCK_URL})
            await asyncio.sleep(1)
            await js(DUCK_NO_HISTORY_JS)  # don't keep IO's chats in Duck.ai's history in your browser
            await s.call_tool("browser_navigate", {"url": DUCK_URL})
            await asyncio.sleep(3)
        self.in_chat = False
        if image and "no-input" in await js(DUCK_ATTACH_JS % json.dumps(base64.b64encode(image).decode())):
            prompt = prompt.replace("A screenshot of the window I'm working in is attached.\n\n", "")
        await asyncio.sleep(2 if image else 0.3)
        if "no-textarea" in await js(DUCK_TYPE_JS % json.dumps(prompt)):
            return "error: Duck.ai's page didn't load"
        await asyncio.sleep(0.5)
        for _ in range(10):  # Ask stays disabled while the picture uploads
            if "ok" in await js(DUCK_SEND_JS):
                break
            await asyncio.sleep(1)
        else:
            await s.call_tool("browser_press_key", {"key": "Enter"})
        text, same = "", 0
        try:
            for _ in range(60):
                await asyncio.sleep(2)
                try:
                    state = json.loads(await js(DUCK_READ_JS))
                except ValueError:
                    continue
                if state.get("limited"):
                    return "error: limit: Duck.ai says its usage limit is reached"
                if state.get("challenge"):
                    return "error: Duck.ai asked to prove a human is there; that's for the user, not IO"
                new = duck_answer(state.get("text", ""))
                same = same + 1 if new and new == text and not state.get("generating") else 0
                text = new
                if same >= 2:
                    break
        finally:
            if keep and text:
                self.in_chat = True
            else:
                try:
                    await js(DUCK_FORGET_JS)
                    await s.call_tool("browser_navigate", {"url": "about:blank"})
                except Exception:
                    pass
        return text[:2000] if text else "error: no answer from Duck.ai"


async def meanings_hint(session, page: str, task: str) -> str:
    """On a page that lists different meanings of a word, show the links that fit the task, so the agent opens the right one."""
    if not DISAMBIGUATION.search(page[:6000]):
        return ""
    try:
        raw = text_of(await session.call_tool("browser_evaluate", {"function": LINKS_JS}))
        m = re.search(r"### Result\s*\n(.*?)(?:\n###|$)", raw, re.S)
        links = json.loads(m.group(1) if m else raw)
    except Exception:
        return ""
    words = [w for w in re.findall(r"\w+", task.lower()) if len(w) > 2 and w not in FIND_STOPWORDS]
    ranked = sorted(links, key=lambda l: -sum(w in l[0].lower() for w in words))
    shown = [f"- {t[:120]} -> {h}" for t, h in ranked[:15]]
    return ("This page is a list of different meanings, not the page you want. Open the link that matches the task with "
            "browser_open (its address), then read that page. Links that best fit the task first:\n" + "\n".join(shown))


def browser_open_tool(mode: str) -> dict:
    where = ("IO's own tab in the user's Chrome, inside the tab group named IO (the first call connects to Chrome)" if mode == "chrome"
             else "IO's own Edge window")
    return {
        "type": "function",
        "function": {
            "name": "browser_open",
            "description": (f"Open a web page in {where}. Start every web task with this, and use it when the user asks you to use "
                            "the browser, Chrome or your tab. Then use browser_snapshot, browser_click and browser_type in that tab."),
            "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "Address to open; leave empty for a blank tab"}}},
        },
    }


def mcp_to_openai(tool, hide: set = HIDDEN_ARGS) -> dict:
    schema = dict(tool.input_schema or {"type": "object", "properties": {}})
    schema.pop("title", None)
    props = {k: v for k, v in schema.get("properties", {}).items() if k not in hide}
    schema["properties"] = props
    if "required" in schema:
        schema["required"] = [r for r in schema["required"] if r in props]
    description = (tool.description or "")[:1200]
    if "loc" in props:
        description += " loc must be a JSON array [x, y] of screen coordinates from Snapshot or find_on_screen."
    return {"type": "function", "function": {"name": tool.name, "description": description, "parameters": schema}}


def fix_args(name: str, args: dict) -> dict:
    """Repair common small-model slips: loc as "(x, y)" text, and stray label args."""
    args = {k: v for k, v in args.items() if k not in HIDDEN_ARGS}
    loc = args.get("loc")
    if isinstance(loc, str):
        nums = re.findall(r"-?\d+(?:\.\d+)?", loc)
        if len(nums) >= 2:
            args["loc"] = [round(float(nums[0])), round(float(nums[1]))]
    elif isinstance(loc, dict):
        loc = {str(k).strip("\"' "): v for k, v in loc.items()}
        if {"x", "y"} <= loc.keys():
            args["loc"] = [round(float(loc["x"])), round(float(loc["y"]))]
    return args


def compact(messages: list[dict], keep: int = 2, trim: int = 1500, snaps_kept: int = KEEP_FULL_SNAPSHOTS, cap: int = 0) -> list[dict]:
    """Keep only the newest tool results in full. Older Snapshots are dropped (huge and stale); other
    older results (web pages, documents, plugin output) are cut short so the context doesn't overflow.
    cap: also cut any single remaining result to this many characters (0 = no limit)."""
    snapshot_ids = {
        tc["id"]
        for m in messages
        if m["role"] == "assistant"
        for tc in m.get("tool_calls") or []
        if tc["function"]["name"] == "Snapshot"
    }
    results = [m for m in messages if m["role"] == "tool"]
    snaps = [m for m in results if m["tool_call_id"] in snapshot_ids]
    others = [m for m in results if m["tool_call_id"] not in snapshot_ids]
    stale_snaps = {id(m) for m in (snaps[:-snaps_kept] if snaps_kept else snaps)}
    stale_others = {id(m) for m in (others[:-keep] if keep else others)}
    out = []
    for m in messages:
        if id(m) in stale_snaps:
            m = {**m, "content": "[older snapshot omitted; call Snapshot again if needed]"}
        elif id(m) in stale_others and len(m["content"]) > trim:
            m = {**m, "content": m["content"][:trim] + "\n[older result trimmed; call the tool again if you need the rest]"}
        elif cap and m["role"] == "tool" and len(m["content"]) > cap:
            m = {**m, "content": m["content"][:cap] + "\n[result cut to fit the model's memory; ask for less (a window, a find) if you need more]"}
        out.append(m)
    return out


# Windows PowerShell 5.1 writes its output in the ANSI code page, so names like "Battlefield™ 6" came back garbled or the
# line went missing, and its first-run progress records arrived as CLIXML noise. The command's output is passed back as
# base64 UTF-8 instead, with progress records off, and files are read as UTF-8 (5.1 assumes ANSI).
PS_WRAP = "$ProgressPreference = 'SilentlyContinue'; $PSDefaultParameterValues['*:Encoding'] = 'utf8'; $__io = & {{\n{cmd}\n}} 2>&1 | Out-String -Width 300; 'IO64:' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($__io))"


def ps_wrap(cmd: str) -> str:
    return PS_WRAP.format(cmd=cmd)


def ps_unwrap(result: str) -> str:
    m = re.search(r"IO64:([A-Za-z0-9+/=]*)", result)
    if not m:
        return result  # e.g. the command didn't parse: PowerShell's own error is the answer
    try:
        text = base64.b64decode(m.group(1)).decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
    except ValueError:
        return result
    status = re.search(r"Status Code: *(-?\d+)", result)
    return f"Response: {text or '(no output)'}\n\nStatus Code: {status.group(1) if status else 0}"


def model_context(default: int = 16384) -> int:
    """The loaded boss model's context size in tokens, from llama-server (it differs per model mode)."""
    try:
        with urllib.request.urlopen(BOSS_URL.rsplit("/v1", 1)[0] + "/props", timeout=3) as r:
            props = json.load(r)
        return int(props.get("default_generation_settings", {}).get("n_ctx") or props.get("n_ctx") or default)
    except Exception:
        return default


# callbacks that receive every log record (the web app streams these to the page)
listeners: list = []
# part of the title of the window the running task works in ('' = none known); IO's focus glow outlines it
focus_hint = ""


def hint_focus(title: str) -> None:
    global focus_hint
    focus_hint = title


def log(event: str, **data) -> None:
    record = {"t": round(time.time(), 2), "event": event, **data}
    line = json.dumps(record, ensure_ascii=False)
    print(line[:600], flush=True)
    for listener in listeners:
        listener(record)
    with open(HERE / "logs" / "boss.jsonl", "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------- memory: short notes that carry across tasks (data/memory.json) ----------

MEMORY = HERE / "data" / "memory.json"


def memory_load() -> list[dict]:
    try:
        return json.loads(MEMORY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def memory_save(notes: list[dict]) -> None:
    MEMORY.parent.mkdir(exist_ok=True)
    MEMORY.write_text(json.dumps(notes, ensure_ascii=False, indent=1), encoding="utf-8")


def remember(text: str) -> str:
    notes = memory_load()
    text = text.strip()
    if text and all(n["text"] != text for n in notes):
        notes.append({"id": hex(int(time.time() * 1000))[2:], "text": text[:500], "created": time.time()})
        memory_save(notes[-MAX_MEMORY:])
    return "remembered"


# ---------- risky actions that need the user's OK when confirm_risky is on ----------

RISKY_POWERSHELL = re.compile(
    r"\b(Remove-Item|rm|del|erase|rmdir|rd|Format-Volume|format|Clear-Content|Stop-Computer|Restart-Computer|shutdown|"
    r"Stop-Process|taskkill|kill|Set-ExecutionPolicy|reg\s+delete|Remove-ItemProperty|Uninstall-\w+|Send-MailMessage)\b",
    re.I,
)


def risky_reason(name: str, args: dict) -> str:
    """Why an action needs confirmation, or "" if it doesn't."""
    if name == "PowerShell" and RISKY_POWERSHELL.search(str(args.get("command", ""))):
        return f"run PowerShell: {args.get('command')}"
    if name == "FileSystem" and (args.get("mode") in ("delete", "move") or (args.get("mode") == "write" and args.get("overwrite"))):
        return f"{args.get('mode')} the file {args.get('path')}"
    if name == "close_windows":
        keep = [k.lower() for k in (args.get("all_except") or [])]
        wanted = [t.lower() for t in (args.get("titles") or [])]
        everything_but = args.get("all_except") is not None
        names = [t for _, t in open_windows()
                 if (not any(k in t.lower() for k in keep) if everything_but else any(w in t.lower() for w in wanted))]
        return "close these windows: " + (", ".join(names) or "(none match)")
    if name == "Process" and args.get("mode") == "kill":
        return f"kill the process {args.get('name') or args.get('pid')}"
    return ""


# plugin tools (notes, databases, git, GitHub...) that change things: by the server's own hint, by name,
# or SQL that writes. Plain reads and SELECTs don't ask, so unattended tasks aren't held up.
PLUGIN_RISKY = re.compile(r"(delete|remove|drop|reset|merge|push|move|rename|overwrite|write|update|edit|commit|create|close|truncate)", re.I)
SQL_WRITE = re.compile(r"\b(delete|drop|update|insert|alter|truncate|replace|create|attach)\b", re.I)


def plugin_risky(alias: str, tool, args: dict) -> str:
    ann = getattr(tool, "annotations", None)
    sql_write = re.search(r"(query|sql|execute)", tool.name, re.I) and any(isinstance(v, str) and SQL_WRITE.search(v) for v in args.values())
    if (ann and getattr(ann, "destructive_hint", False)) or PLUGIN_RISKY.search(tool.name) or sql_write:
        return f"use {alias} with {json.dumps(args, ensure_ascii=False)[:200]}"
    return ""


def image_part(path: Path) -> dict:
    """An attached image as an OpenAI-style content part, downscaled so it stays cheap for the local model.
    Phone photos are turned upright, and transparent areas become white instead of black."""
    from PIL import Image, ImageOps

    with Image.open(path) as src:
        im = ImageOps.exif_transpose(src)
        im.thumbnail((1600, 1600))
        if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, "white")
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        else:
            im = im.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=88)
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}}


def clean_summary(text: str) -> str:
    """Drops bookkeeping a small model sometimes copies into its answer from the chat history."""
    text = re.sub(r"\s*\[actions taken:.*?\]\s*$", "", text or "", flags=re.S)
    text = re.sub(r"\s*\(context, not part of the request:.*?\)\s*$", "", text, flags=re.S)
    text = re.sub(r"\n\s*done\.?\s*$", "", text, flags=re.I)  # a stray "done" line after the answer
    return text.strip()


EMPTY_SUMMARY = re.compile(r"^(done|ok|okay|finished|complete|completed|task (is )?(done|complete|completed)|"
                           r"i have described .*|i described .*|i have (found|answered) .*)[.!]?$", re.I | re.S)


def fill_empty(summary: str, last_info: str) -> str:
    """'done' or 'I have described it' isn't an answer: show the last thing the tools found instead."""
    if last_info and (len(summary) < 8 or EMPTY_SUMMARY.match(summary.strip())):
        return last_info.strip()[:2000]
    return summary


CHECK_SYSTEM = """You check the work of a Windows assistant before it reports back to the user.
Given the user's request, the actions it took with their results, and the answer it wants to give, decide whether
the user's request (not any plan) was really done. Claiming an action happened when it failed, skipping something the
user asked for, or stating live facts (times, files, screen contents, page contents) the results don't show mean it is
not done. Answering a general-knowledge question from knowledge is fine, and so is honestly saying a tool failed.
If the results reasonably support the answer, say YES: don't ask for extra proof the user didn't want.
Reply with exactly YES, or NO: followed by one sentence saying what is missing or wrong and what to do next."""

SUMMARY_SYSTEM = """You compress the working notes of a Windows assistant so it can keep going with less to read.
Summarize the earlier steps below in at most 150 words: what has been done, what was found (exact values, names,
paths, URLs, coordinates and refs it may still need), and what failed. Don't add advice. Output only the summary."""
MAX_REDOS = 2
COMPACT_AT = 40000  # characters of working messages before older steps are summarized
KEEP_RECENT = 6  # newest messages always kept word for word

# Loop mode: a standing goal IO keeps working on until you press Stop. It never finishes: "done" is a progress report,
# the goal stays pinned at the top of its context, and older steps are folded into a running summary much sooner, so
# it can go on indefinitely inside the local model's context.
LOOP_REQUEST = re.compile(r"^\s*/loop\b|\buntil i (tell (you|it|io) to |say (to )?|ask (you|it) to )?stop\b|\buntil i stop (you|it)\b|\b(forever|endlessly|"
                          r"indefinitely|non-?stop|on (a )?loop)\b|\bkeep (going|playing|doing (it|this|that)) until\b", re.I)
LOOP_COMPACT_AT = 8000
# a loop on one window gets just the tools for acting in it: a smaller prompt (the 16K-context models need the room)
# and no two-step find-then-click habit
LOOP_TOOLS = {"click_on", "hold_on", "look_at_screen", "wait", "Scroll", "Shortcut", "type_text", "App", "research", "done"}
LOOP_KEEP_RECENT = 6
LOOP_REPEAT_LIMIT = 25
LOOP_RESEARCH_EVERY = 20  # steps without research before a loop looks up whatever it's working on now  # the same call this many times in a row is a rut, even in a game
LOOP_NOTE = """LOOP MODE (the user approved it): this goal has no end. Keep working toward it, action after action, until the
user presses Stop. You are never finished, so never stop to ask the user anything: decide for yourself.
- Things change while you work: look again (look_at_screen / find_on_screen) before acting on old information.
- Act with click_on and hold_on (they find and act in one step). Never guess coordinates.
- Use wait when something needs time (a timer, an animation, resources building up), with the seconds you think it needs.
- Stuck, or don't know how something works? Call research with a question (it reads guides without leaving the app).
- Calling done only records a short progress note (what you did, what changed, what you'll do next); then you carry on.
- If something isn't working, try a different approach instead of repeating it.
- Older steps get folded into a progress summary to save space; the goal above always stays."""


def loop_goal(task: str) -> str:
    return re.sub(r"^\s*/loop\b\s*", "", task).strip() or task


def local_chat(system: str, user: str, max_tokens: int = 300, think: bool = True) -> str:
    client = OpenAI(base_url=BOSS_URL, api_key="local", max_retries=1, timeout=90)
    reply = client.chat.completions.create(model=BOSS_MODEL, temperature=0.1, max_tokens=max_tokens, extra_body=None if think else NO_THINKING,
                                           messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    return re.sub(r"<think>.*?</think>", "", reply.choices[0].message.content or "", flags=re.S).strip()


RESOLVE_SYSTEM = """You turn the user's latest chat message into a standalone request, using the conversation before it.
Resolve words like "it", "that", "the moon", "again", "what about..." from what was being discussed (for example after
"What is IO?" answered about the assistant IO, "what about the moon?" means "What is Io, the moon of Jupiter?").
Keep the user's intent and wording otherwise. If the message already stands alone, return it unchanged.
Output only the rewritten request, one line."""


def resolve_followup(task: str, conversation: list[dict]) -> str:
    turns = []
    for m in conversation[-8:]:
        content = m["content"] if isinstance(m["content"], str) else ""
        turns.append(f"{m['role']}: {clean_summary(content)[:400]}")
    text = local_chat(RESOLVE_SYSTEM, "Conversation:\n" + "\n".join(turns) + f"\n\nLatest message: {task}", max_tokens=80)
    return text.splitlines()[0].strip().strip('"') if text else task


def check_work(task: str, steps: list[str], answer: str) -> str:
    """'' when the work looks done; otherwise what is missing, in one sentence."""
    verdict = local_chat(CHECK_SYSTEM, f"Request: {task}\n\nActions and results:\n" + "\n".join(steps[-8:]) +
                         f"\n\nAnswer it wants to give:\n{answer[:1500]}", max_tokens=120)
    if verdict.upper().startswith("NO"):
        return verdict[2:].lstrip(" :.-") or "the request doesn't look done yet"
    return ""


def summarize_steps(task: str, old: list[dict]) -> str:
    lines = []
    for m in old:
        if m["role"] == "assistant":
            calls = ", ".join(f"{tc['function']['name']}({tc['function']['arguments'][:200]})" for tc in m.get("tool_calls") or [])
            lines.append(f"assistant: {str(m.get('content') or '')[:300]} {calls}".strip())
        elif m["role"] == "tool":
            lines.append(f"result: {str(m['content'])[:600]}")
        else:
            lines.append(f"note: {str(m['content'])[:400]}")
    return local_chat(SUMMARY_SYSTEM, f"Task: {task}\n\nEarlier steps:\n" + "\n".join(lines), max_tokens=320)


def text_of(res) -> str:
    text = "\n".join(getattr(p, "text", "") for p in res.content if getattr(p, "type", "") == "text")
    if getattr(res, "is_error", False):  # many servers report failures this way, with text that doesn't say "error"
        return "error: " + (text or "the tool reported a failure")
    return text


def inline_browser_snapshot(result: str, cwd: Path) -> str:
    """Playwright MCP writes page snapshots to .yml files; inline them so the boss can read the page."""
    m = re.search(r"\[Snapshot\]\(([^)]+\.yml)\)", result)
    if not m:
        return result
    try:
        page = (cwd / m.group(1)).read_text(encoding="utf-8")
    except OSError:
        return result
    return result.replace(m.group(0), "\n" + page[:MAX_TOOL_TEXT])


async def desktop_context(win: ClientSession) -> str:
    """Focused and open windows from a Snapshot, for the planner (no UI tree, no screenshot)."""
    try:
        raw = text_of(await win.call_tool("Snapshot", {"use_vision": False, "use_annotation": False}))
        raw = "\n".join(json.loads(raw)) if raw.startswith("[") else raw
    except Exception:
        return ""
    m = re.search(r"Focused Window:(.*?)Opened Windows:(.*)", raw.split("UI Tree:")[0], re.S)
    return f"Focused window:{m.group(1).rstrip()}\nOpen windows:{m.group(2).rstrip()}"[:2500] if m else ""


async def run(task: str, max_steps: int, options: dict | None = None, ask=None, conversation: list[dict] | None = None,
              images: list[Path] | None = None) -> str:
    """Runs one task (see _run); the focus hint is cleared however it ends."""
    hint_focus("")
    try:
        return await _run(task, max_steps, options, ask, conversation, images)
    finally:
        hint_focus("")


async def _run(task: str, max_steps: int, options: dict | None = None, ask=None, conversation: list[dict] | None = None,
               images: list[Path] | None = None) -> str:
    """Runs one task. options: allow_powershell, confirm_risky, browser, files (all bools).
    ask: async callable(question) -> answer, used by ask_user and risky-action confirmations.
    conversation: earlier turns of the chat this message belongs to, as [{"role": "user"|"assistant", "content"}],
    so follow-ups like "now do the same for the other one" make sense."""
    conversation = conversation or []
    loop = bool((options or {}).get("loop"))
    if loop:
        task = loop_goal(task)
    options = {
        "allow_powershell": True, "confirm_risky": True, "browser": True, "files": True,
        **(options or {}),
    }
    (HERE / "logs").mkdir(exist_ok=True)
    boss = OpenAI(base_url=BOSS_URL, api_key="local", max_retries=3, timeout=300)
    eyes = Eyes(options.get("model_mode", "fast"))

    windows_tools = MCP_TOOLS.split(",")
    if not options["allow_powershell"]:
        windows_tools.remove("PowerShell")
    if not options["files"]:
        windows_tools = [t for t in windows_tools if t not in ("FileSystem", "Scrape")]
    browser_dir = HERE / "data" / "browser"
    browser_dir.mkdir(parents=True, exist_ok=True)

    async with AsyncExitStack() as stack:
        win_r, win_w = await stack.enter_async_context(stdio_client(
            # through the venv interpreter, not the windows-mcp.exe launcher, which has the venv path baked in
            # with IO's purple focus glow on, Windows-MCP's orange-red after-screenshot flash would clash with it
            StdioServerParameters(command=str(Path(sys.executable).with_name("python.exe")), args=["-m", "windows_mcp", "serve", "--tools", ",".join(windows_tools)],
                                  env={"WINDOWS_MCP_DISABLE_FLASH": "1"} if options.get("focus_glow") else None),
            errlog=sys.stderr,
        ))
        win = await stack.enter_async_context(ClientSession(win_r, win_w))
        await win.initialize()
        mcp_tools = list((await win.list_tools()).tools)
        sessions = {t.name: win for t in mcp_tools}
        if options["browser"]:
            try:
                br_r, br_w = await stack.enter_async_context(stdio_client(browser_params(options), errlog=sys.stderr))
                br = await stack.enter_async_context(ClientSession(br_r, br_w, client_info=BROWSER_CLIENT))
                await br.initialize()
                # in your own Chrome, closing is left to you
                allowed = BROWSER_TOOLS - {"browser_close"} if options.get("browser_mode") == "chrome" else BROWSER_TOOLS
                for t in (await br.list_tools()).tools:
                    if t.name in allowed:
                        sessions[t.name] = br
                        mcp_tools.append(t)
            except Exception as e:
                log("warning", text=f"browser tools unavailable: {e}")

        # installed plugins from the Customize page, exposed as "<plugin>_<tool>"
        plugin_tools = await plugins.start_enabled(stack, log=lambda m: log("warning", text=m))
        plugin_defs = {}
        for alias, _, t in plugin_tools:
            d = mcp_to_openai(t, hide=set())  # label/labels are only hidden for Windows-MCP
            d["function"]["name"] = alias
            plugin_defs[alias] = d
        # the boss has a 32K context: if the enabled plugins' tool lists are too big, keep the ones the task mentions
        plugin_tools = plugins.fit_budget(plugin_tools, plugin_defs, task, log=lambda m: log("warning", text=m))
        aliases, plugin_meta = {}, {}
        for alias, session, t in plugin_tools:
            sessions[alias] = session
            aliases[alias] = t.name
            plugin_meta[alias] = t
        extra = [t for t in EXTRA_TOOLS if (ask and not loop) or t["function"]["name"] != "ask_user"]
        if "browser_navigate" in sessions:
            extra += [browser_open_tool(options.get("browser_mode", "edge")), BROWSER_READ_TOOL]
        tools = [mcp_to_openai(t) for t in mcp_tools] + [plugin_defs[alias] for alias, _, _ in plugin_tools] + extra
        notes = memory_load()
        skills = plugins.skills_prompt(task)
        prompt = task
        if notes or skills:
            # next to the task, where a small model actually reads it (it overlooks notes in the system prompt)
            memo = "\n".join(f"- {n['text']}" for n in notes[-MAX_MEMORY:])
            prompt = (f"Your saved notes (use them when relevant):\n{memo}\n\n" if notes else "") + (f"{skills}\n\n" if skills else "") + f"Task: {task}"
        standalone = task
        if conversation:
            try:
                standalone = await asyncio.to_thread(resolve_followup, task, conversation) or task
            except Exception as e:
                log("warning", text=f"couldn't resolve the follow-up: {e}")
            if standalone.strip().lower() != task.strip().lower():
                log("resolved", text=standalone)
                prompt += f"\n\n(This continues the conversation above. In context, the user means: {standalone})"
            else:
                prompt += "\n\n(This continues the conversation above; resolve words like 'it', 'that', or 'again' from it.)"
        parts = []
        for p in images or []:
            try:
                parts.append(await asyncio.to_thread(image_part, Path(p)))
            except Exception as e:  # an unreadable file shouldn't sink the rest of the request
                log("warning", text=f"couldn't read image {Path(p).name}: {e}")
        images = parts
        if loop:
            prompt += "\n\n" + LOOP_NOTE
        if images:
            where = "earlier in this chat" if options.get("images_from_earlier") else "to this message"
            prompt += (f"\n\n(The user attached {len(images)} image(s) {where}; they are shown above this text. They are the user's own "
                       "images, not your screen: answer questions about them directly from what you see, without tools. Only use tools if the "
                       "task also asks you to do something on the PC.)")
            user_content = [*images, {"type": "text", "text": prompt}]
        else:
            user_content = prompt
        if "browser_navigate" in sessions:
            system = SYSTEM.replace("{browser_where}", BROWSER_WHERE.get(options.get("browser_mode", "edge"), BROWSER_WHERE["edge"]))
        else:  # browser off or failed to start: point web work at Scrape or the desktop tools instead
            system = re.sub(r"- For anything on a website.*?\n", "- For websites, use Scrape to read a page as text; to interact with one, "
                            "launch the browser with App and use Snapshot, Click and Type.\n", SYSTEM, count=1)
            system = system.replace(" It sees the PC's monitors, not IO's browser tab: answer questions about a web page from browser_snapshot "
                                    "(its title, headings and text).", "")
        messages: list[dict] = [{"role": "system", "content": system}, *conversation, {"role": "user", "content": user_content}]
        head = len(messages)  # everything after this is the task's own working notes, which can be summarized
        steps_log: list[str] = []  # every action with its result, for checking the work before answering
        redos = 0
        log("start", task=task, tools=[t["function"]["name"] for t in tools])
        extra_tools = "\n".join(f"- {alias}: {(t.description or '').split('. ')[0][:120]}" for alias, _, t in plugin_tools)
        last_error, refused_done = "", False
        repeat = {"key": "", "n": 0}  # the same call over and over with nothing in between is a loop
        said_more = False
        browser_tab_open = False  # set once browser_open has opened IO's tab in this task
        recent: list[str] = []  # recent call keys, to spot a snapshot/read/snapshot/read loop
        last_info = ""  # the last answer-like tool result, used when the model's own summary says nothing
        actions: list[str] = []  # short action/result lines, for replanning
        error_streak, replans, last_plan_step, plain_replies = 0, 0, 0, 0

        async def check_before_done(answer: str) -> str:
            """Before reporting back after doing things, check the work; if it falls short, say so and keep going.
            At most MAX_REDOS times per task, and never for plain chat (no tools used)."""
            nonlocal redos
            if not steps_log or redos >= MAX_REDOS:
                return ""
            try:
                problem = await asyncio.to_thread(check_work, task, steps_log, answer)
            except Exception as e:
                log("warning", text=f"couldn't check the work: {e}")
                return ""
            if not problem:
                return ""
            redos += 1
            log("check", text=problem)
            return f"Not done yet: {problem} Fix it, then give your answer again."

        async def get_plan(history: str = "") -> None:
            """The local boss writes itself a plan (and a new one when stuck). Everything stays on this PC, so it gets
            the full picture: open windows, saved notes, the conversation, extra tools and skills."""
            parts = [await desktop_context(win)] if history else []
            if notes:
                parts.append("Saved notes:\n" + "\n".join(f"- {n['text']}" for n in notes[-MAX_MEMORY:]))
            if conversation:
                asked = [clean_summary(m["content"])[:300] for m in conversation[-6:] if m["role"] == "user" and isinstance(m["content"], str)]
                if asked:
                    parts.append("Earlier requests in this conversation:\n" + "\n".join(f"user: {a}" for a in asked))
            if "browser_navigate" not in sessions:
                parts.append("The agent has no browser tools this time: plan Scrape to read a page, or App, Snapshot, Click and Type to use a browser window.")
            else:
                where = "IO's own tab in the user's Chrome (tab group 'IO')" if options.get("browser_mode") == "chrome" else "IO's own Edge window"
                parts.append(f"Web pages: browser_open(url) opens {where}; then browser_read, browser_snapshot, browser_click and browser_type work in it.")
            if extra_tools:
                parts.append("Extra tools the agent has (prefer them when they fit):\n" + extra_tools)
            if skills:
                parts.append(skills)
            context = "\n\n".join(p for p in parts if p)
            try:
                t0 = time.time()
                text = await asyncio.to_thread(planner.plan_local, standalone, context, BOSS_URL, BOSS_MODEL, history)
            except Exception as e:
                log("warning", text=f"planning failed: {e}")
                return
            if text:
                messages.append({"role": "user", "content": f"Your plan:\n{text}\n\nFollow it step by step, adapting to what you find."})
            log("plan", source="local", plan=text, secs=round(time.time() - t0, 1), reason="" if text else "conversation, no plan needed")

        if loop:
            # you confirm every loop before it starts: it keeps acting on the PC until you press Stop
            if ask:
                answer = await ask(f"Start a loop? I'll keep working on this until you press Stop: {task} (yes/no)")
                if not answer.strip().lower().startswith("y"):
                    log("done", step=0, summary="Loop not started.")
                    return "Loop not started."
            focus = await asyncio.to_thread(window_in, task)
            if focus:
                hint_focus(focus)
                # every screenshot, find and click stays inside this window's content, so nothing beside it gets hit
                eyes.content = True
                eyes.brief = True
                tools[:] = [t for t in tools if t["function"]["name"] in LOOP_TOOLS]
                prompt_note = (f"\nThe goal is in the '{focus}' window. find_on_screen and look_at_screen only see its content area, and "
                               "clicks outside it are blocked.")
                if isinstance(messages[head - 1]["content"], str):
                    messages[head - 1]["content"] += prompt_note
            log("loop", goal=task, window=focus)
            tools.append(RESEARCH_TOOL)
            researcher = Researcher(stack)
            how = options.get("gemini_mode", "private")
            token = options.get("chrome_token", "")
            if not options.get("ask_gemini") or (how in ("account", "duck") and not token):
                gemini = None
            elif how == "duck":
                gemini = DuckAI(stack, token)
            else:
                gemini = Gemini(stack, token, how != "account")
            director = bool(gemini) and options.get("advisor_role", "director") == "director"
            if gemini and not director:
                tools.append(ASK_GEMINI_TOOL)
                if isinstance(messages[head - 1]["content"], str):
                    messages[head - 1]["content"] += ("\n- When research hasn't helped and you're still stuck, call ask_gemini with a specific question "
                                                      "(a stronger model).")

            def window_shot() -> bytes:
                """The loop's window (its content area), as a JPEG small enough to paste."""
                area = content_rect(focus) if focus else None
                if not area:
                    return b""
                shot = ImageGrab.grab(bbox=area, all_screens=True).convert("RGB")
                shot.thumbnail((1280, 1280))
                buf = io.BytesIO()
                shot.save(buf, format="JPEG", quality=80)
                return buf.getvalue()

            async def consult(question: str) -> str:
                # signed-out Gemini takes no uploads: private asks go with the screen described in words instead
                image = await asyncio.to_thread(window_shot) if gemini.takes_images else b""
                prompt = fit_advisor_prompt(gemini.max_chars, shot="A screenshot of the window I'm working in is attached.\n\n" if image else "",
                                            goal=task, guide="\n---\n".join(guide[-2:]) or "(none yet)", screen=last_info or "(not looked yet)",
                                            actions="\n".join(actions[-10:]) or "(none yet)", question=question)
                t0 = time.time()
                try:
                    answer = await gemini.ask(prompt, image)
                except Exception as e:
                    answer = f"error: couldn't reach Gemini: {e}"
                if focus:
                    await asyncio.to_thread(focus_window, focus)  # Chrome may have come to the front: back to the app
                log("gemini", question=question, via=type(gemini).__name__, secs=round(time.time() - t0, 1), answer=answer[:800])
                if not answer.startswith("error"):
                    guide.append(f"(Advisor on: {question}) {answer[:900]}")
                    pin_guide()
                return answer
            guide: list[str] = []

            def pin_guide() -> None:
                """Keeps the latest research next to the goal, where compaction never reaches."""
                base = messages[head - 1]
                if isinstance(base["content"], str):
                    base["content"] = base["content"].split("\n\nWhat you learned from research:")[0] + \
                        "\n\nWhat you learned from research:\n" + "\n---\n".join(guide[-3:])

            # research mode first: learn how the thing works before acting on it
            # (a director already knows how things work: it only needs the screen and the tools)
            try:
                if not director:
                    t0 = time.time()
                    q = await asyncio.to_thread(local_chat, ("Write one Google search query (under 10 words) that finds a beginner guide for this goal. Name the game or app "
                                                 "itself, not the program or emulator it runs in. Output only the query."),
                                                task, 40, False)
                    brief = await researcher.ask(q.strip().strip('"') or task, task)
                    if not brief.startswith("error"):
                        guide.append(brief)
                        pin_guide()
                    log("research", question=q, secs=round(time.time() - t0, 1), notes=brief[:600])
            except Exception as e:
                log("warning", text=f"couldn't research the goal first: {e}"[:300])
            if gemini and not director:
                # the stronger model plans the start, like the planner did for single tasks: it sees the window and the research
                plan = await consult("I'm just starting. " + ("Look at the screenshot and give" if gemini.takes_images else "Give")
                                     + " me a numbered plan for my first steps toward the goal.")
                if not plan.startswith("error"):
                    messages.append({"role": "user", "content": f"Plan from a stronger model (it saw the window):\n{plan[:1500]}\nFollow it, adapting to what you see."})
        last_research = researches = 0
        if not loop:
            gemini, director = None, False
        director_queue: list[tuple[str, dict]] = []
        director_paused_until = 0.0
        director_seen = director_rounds = 0

        async def direct(step: int) -> str:
            """Director mode: asks the stronger model for the next actions and queues them; returns its thoughts ('' on failure)."""
            nonlocal director_seen, director_rounds
            image = await asyncio.to_thread(window_shot) if gemini.takes_images else b""
            keep = isinstance(gemini, DuckAI)
            # one ongoing conversation, so it remembers what it tried: after the first round only the new results go in.
            # A fresh conversation (with the full brief) when the old one fails or has grown long.
            if keep and gemini.in_chat and director_rounds % 20:
                new = steps_log[director_seen:] or ["(no actions ran)"]
                prompt = ("Results of your last actions:\n" + "\n".join(re.sub(r"\s+", " ", a)[:320] for a in new)[-3500:] +
                          ("\nA new screenshot is attached." if image else f"\nIO's eyes now see: {last_info[:500]}") +
                          "\nIf the same thing keeps not working, change approach. Reply with the next JSON only.")
            else:
                prompt = fit_director_prompt(
                    gemini.max_chars, goal=task, shot="A screenshot of the window IO works in is attached.\n" if image else "", guide="",
                    screen="" if image else f"What IO's eyes last saw on screen:\n{last_info or '(nothing yet)'}\n\n",
                    history="\n".join(re.sub(r"\s+", " ", a)[:320] for a in steps_log[-14:]) or "(none yet: this is the start)",
                    catalog=tool_catalog(tools))
            director_seen, director_rounds = len(steps_log), director_rounds + 1
            t0 = time.time()
            try:
                reply = await (gemini.ask(prompt, image, keep=True) if keep else gemini.ask(prompt, image))
                if keep and reply.startswith("error") and not reply.startswith("error: limit"):
                    director_rounds = 0  # start over in a new conversation next time
            except Exception as e:
                reply = f"error: {e}"
            if focus:
                await asyncio.to_thread(focus_window, focus)  # Chrome may have come to the front: back to the app
            thoughts, batch = ("", []) if reply.startswith("error") else parse_director(reply, {t["function"]["name"] for t in tools})
            log("director", step=step, via=type(gemini).__name__, secs=round(time.time() - t0, 1), thoughts=thoughts,
                actions=[f"{n}({json.dumps(a, ensure_ascii=False)[:100]})" for n, a in batch], error=reply[:300] if not batch else "")
            if reply.startswith("error: limit"):
                nonlocal director_paused_until
                director_paused_until = time.time() + 1800
                log("progress", step=step, n=0, summary="Duck.ai's usage limit was reached: Qwen decides on its own for 30 minutes, then IO asks Duck.ai again.")
            director_queue.extend(batch)
            return (thoughts or "(director)") if batch else ""

        # attached images: the boss answers from what it sees instead of planning; small talk needs no plan
        small_talk = len(task.split()) <= 6 and bool(SMALL_TALK.match(task.strip()))
        if not images and not small_talk:
            await get_plan()
        compact_at, keep_recent = (LOOP_COMPACT_AT, LOOP_KEEP_RECENT) if loop else (COMPACT_AT, KEEP_RECENT)
        # what fits: the model's context (16K for Qwen 3.6, 32K for the others) less the fixed prompt, the tool list and the
        # reply, at ~2.5 characters a token (UI trees and JSON tokenize worse than prose)
        fixed = len(json.dumps(tools)) + sum(len(str(m.get("content") or "")) for m in messages[:head])
        room = max(6000, int((await asyncio.to_thread(model_context) - 1400) * 2.5) - fixed)
        compact_at = min(compact_at, int(room * 0.6))
        snaps_kept = KEEP_FULL_SNAPSHOTS if room > 40000 else 1
        tool_cap = min(MAX_TOOL_TEXT, room // 3)
        progress_notes = 0
        if not loop:
            focus = ""
        found_points: list[tuple[int, int]] = []  # recent find_on_screen answers: loop clicks must come from one

        for step in (itertools.count(1) if loop else range(1, max_steps + 1)):
            if loop and not director and step - last_research >= LOOP_RESEARCH_EVERY and last_info:
                # research mode again: look up whatever the latest look says it's facing, so it doesn't circle
                last_research = step
                try:
                    t0 = time.time()
                    q = await asyncio.to_thread(local_chat, "An assistant is working on the goal below and has seen the screen described below. Write "
                                                "one Google search query (under 12 words) that would explain how to make progress on what the screen "
                                                "shows now (name the game or app itself). Output only the query.",
                                                f"Goal: {task}\nScreen: {last_info[:800]}", 40, False)
                    brief = await researcher.ask(q.strip().strip('"'), task)
                    if not brief.startswith("error"):
                        guide.append(f"({q.strip()}) {brief}")
                        pin_guide()
                        messages.append({"role": "user", "content": f"Research on what you're facing now ({q.strip()}):\n{brief}\nUse it."})
                    log("research", question=q, secs=round(time.time() - t0, 1), notes=brief[:600])
                except Exception as e:
                    log("warning", text=f"couldn't research: {e}"[:300])
                researches += 1
                if gemini and not director and researches >= 2 and not gemini.ready_in():
                    advice = await consult(f"I've been at this for {step} steps and keep circling. What should I do next to make progress?")
                    if not advice.startswith("error"):
                        messages.append({"role": "user", "content": f"Advice from a stronger model:\n{advice[:900]}\nFollow it."})
            if loop:  # replan when stuck, as often as needed, but not on a timer: the goal never ends
                stuck = error_streak >= REPLAN_AFTER_ERRORS and step - last_plan_step > REPLAN_AFTER_STEPS
            else:
                stuck = (error_streak >= REPLAN_AFTER_ERRORS or step - last_plan_step > REPLAN_AFTER_STEPS) and replans < MAX_REPLANS
            if stuck:
                replans, last_plan_step, error_streak = replans + 1, step, 0
                await get_plan("\n".join(actions[-12:]))
            if sum(len(str(m.get("content") or "")) for m in messages[head:]) > compact_at:
                cut = len(messages) - keep_recent
                while cut > head and messages[cut]["role"] != "assistant":  # never split a call from its result
                    cut -= 1
                if cut - head >= 4:
                    try:
                        summary_text = await asyncio.to_thread(summarize_steps, task, messages[head:cut])
                        messages[head:cut] = [{"role": "user", "content": f"Progress so far (older steps summarized to save space):\n{summary_text}"}]
                        log("compact", step=step, text=summary_text)
                    except Exception as e:
                        log("warning", text=f"couldn't summarize older steps: {e}")
            t0 = time.time()
            pending = None  # director mode: the next queued action stands in for the local model's choice
            if director:
                if not director_queue and time.time() < director_paused_until:
                    thoughts = ""  # Duck.ai hit its usage limit: Qwen decides until the pause is over
                else:
                    thoughts = "" if director_queue else await direct(step)
                if director_queue:
                    tool_name, tool_args = director_queue.pop(0)
                    call = SimpleNamespace(id=f"director-{step}", function=SimpleNamespace(name=tool_name, arguments=json.dumps(tool_args)))
                    pending = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=thoughts, tool_calls=[call]))])
            # model calls run in a thread so the web app's event loop stays responsive
            try:
                if pending is not None:
                    response = pending
                elif loop:
                    try:
                        response = await asyncio.to_thread(
                            boss.chat.completions.create,
                            model=BOSS_MODEL, messages=compact(messages, keep=1, trim=800, snaps_kept=1, cap=tool_cap), tools=tools,
                            temperature=0.3, max_tokens=1024,
                        )
                    except Exception as e:
                        log("warning", text=f"loop step {step} failed, retrying: {e}"[:300])
                        if "context" in str(e).lower() and len(messages) - head > 2:
                            messages[head:len(messages) - 2] = [{"role": "user", "content": "Progress so far: (older steps dropped to save space)"}]
                            while len(messages) > head + 1 and messages[head + 1]["role"] == "tool":
                                messages.pop(head + 1)  # never start with a result whose call was dropped
                        await asyncio.sleep(10)
                        continue
                else:
                    response = await asyncio.to_thread(
                    boss.chat.completions.create,
                    model=BOSS_MODEL, messages=compact(messages, snaps_kept=snaps_kept), tools=tools, temperature=0.2, max_tokens=1024,
                )
            except BadRequestError as e:
                if "context" not in str(e).lower():
                    raise
                # over the model's memory: cut harder, then fold everything but the last step into a summary, then give up
                log("warning", text=f"step {step} was over the model's context; trimming and retrying")
                response = None
                for attempt in range(2):
                    if attempt == 1:
                        cut = len(messages) - 2
                        while cut > head and messages[cut]["role"] != "assistant":
                            cut -= 1
                        if cut - head < 1:
                            break
                        try:
                            summary_text = await asyncio.to_thread(summarize_steps, task, compact(messages[head:cut], keep=0, trim=300, snaps_kept=0))
                        except Exception:
                            summary_text = "(older steps dropped to save space)"
                        messages[head:cut] = [{"role": "user", "content": f"Progress so far (older steps summarized to save space):\n{summary_text}"}]
                        log("compact", step=step, text=summary_text)
                    try:
                        response = await asyncio.to_thread(
                            boss.chat.completions.create,
                            model=BOSS_MODEL, messages=compact(messages, keep=1, trim=400, snaps_kept=1, cap=min(tool_cap, 4000)), tools=tools,
                            temperature=0.2, max_tokens=1024,
                        )
                        break
                    except BadRequestError as e2:
                        if "context" not in str(e2).lower():
                            raise
                if response is None:
                    raise RuntimeError("this step needs more memory than the local model has, even after trimming; try a narrower request, "
                                       "or turn off some plugins or skills")
            msg = response.choices[0].message
            calls = msg.tool_calls or []
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                        for c in calls
                    ],
                }
            )
            log("think", step=step, secs=round(time.time() - t0, 1), text=msg.content or "")
            if not calls:
                # small models sometimes write the call as text, e.g. done(summary="..."), or just answer
                text = (msg.content or "").strip()
                # also Gemma's leaked raw form: done{summary:<|"|>...<|"|>}
                m = re.search(r"done\s*[({]\s*summary\s*[=:]\s*(<\|\"\|>|[\"'])(.*?)\1\s*[)}]", text, re.S)
                low = intent_text(text)
                announcing = re.match(r"(i will|i'll|let me|next,|now i|first,|i am going to|i'm going to)\b", low) or \
                    re.search(r"\b(i will|i'll|let me|i am going to|i'm going to) (now )?(try|check|look|search|read|open|scroll)\b", low)
                if loop and text:
                    # a plain reply in a loop is a progress note, not an ending
                    progress_notes += 1
                    log("progress", step=step, n=progress_notes, summary=clean_summary(m.group(2) if m else text)[:600])
                    messages.append({"role": "user", "content": f"Keep going with the goal: {task}\nUse a tool for your next action."})
                    continue
                if m or (text and (plain_replies >= 1 or not announcing)):
                    summary = m.group(2).strip() if m else text
                    summary = fill_empty(clean_summary(summary), last_info)
                    problem = await check_before_done(summary)
                    if problem:
                        messages.append({"role": "user", "content": problem})
                        continue
                    log("done", step=step, summary=summary)
                    return summary
                plain_replies += 1
                messages.append({"role": "user", "content": "Use a tool, or call the done tool with your answer if the task is complete."})
                continue
            plain_replies = 0

            for c in calls:
                name = c.function.name
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                t1 = time.time()
                key = name + json.dumps(args, sort_keys=True, ensure_ascii=False)
                recent.append(key)
                if loop and name == "done":
                    progress_notes += 1
                    note = clean_summary(str(args.get("summary", "")))[:600]
                    log("progress", step=step, n=progress_notes, summary=note)
                    result = f"Progress noted. The loop continues: keep working on the goal ({task}). Look at the screen for what changed, then act."
                    messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
                    continue
                if loop and name != "done":
                    # games and dashboards repeat on purpose: only a long run of the exact same call is a rut
                    repeat["n"] = repeat["n"] + 1 if key == repeat["key"] else 1
                    repeat["key"] = key
                    if repeat["n"] >= LOOP_REPEAT_LIMIT:
                        repeat["n"] = 0
                        result = (f"You have made this exact call {LOOP_REPEAT_LIMIT} times in a row. Check the screen with look_at_screen "
                                  "and try something different that moves the goal forward.")
                        log("tool", step=step, name=name, args=args, result=result)
                        messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
                        continue
                elif name in LOOP_PRONE and len(recent) >= 6 and all(k.split("{")[0] in LOOP_PRONE for k in recent[-6:]) and len(set(recent[-6:])) <= 3:
                    recent.clear()
                    result = ("You keep re-reading the same page without getting closer. Do something different: open a more specific page "
                              "(for example the exact article, like https://en.wikipedia.org/wiki/Io_(moon)), click a link by its ref from "
                              "browser_snapshot, or answer with what you have.")
                    log("tool", step=step, name=name, args=args, result=result)
                    messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
                    continue
                if not loop:
                    repeat["n"] = repeat["n"] + 1 if key == repeat["key"] else 1
                    repeat["key"] = key
                if not loop and name != "done" and repeat["n"] >= (3 if name in LOOP_PRONE else 10):
                    result = (f"You have made this exact call {repeat['n'] - 1} times in a row. Use what you have, try another way "
                              "(for a fact on a long web page, browser_read with find), or call done.")
                    log("tool", step=step, name=name, args=args, result=result)
                    messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
                    last_error = result  # a done right after this is challenged once
                    error_streak += 1
                    continue
                if name == "done":
                    if last_error and not refused_done:
                        # small models like to declare victory right after a failed call
                        refused_done = True
                        result = f"Not done: your last action failed ({last_error[:200]}). Fix it, or call done again explaining why it can't be done."
                        log("tool", step=step, name=name, args=args, result=result)
                        messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
                        continue
                    summary = fill_empty(clean_summary(str(args.get("summary", ""))), last_info)
                    if not said_more and step < max_steps and MORE_TO_DO.search(intent_text(summary)):
                        # "...I will try searching for X" isn't an answer: hold it to that once
                        said_more = True
                        result = "Not done: you said you would try something else. Do it now, then call done with the answer."
                        log("tool", step=step, name=name, args=args, result=result)
                        messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
                        continue
                    problem = await check_before_done(summary)
                    if problem:
                        log("tool", step=step, name=name, args=args, result=problem)
                        messages.append({"role": "tool", "tool_call_id": c.id, "content": problem})
                        continue
                    log("done", step=step, summary=summary)
                    return summary

                if not options["confirm_risky"]:
                    reason = ""
                elif name in plugin_meta:
                    reason = plugin_risky(name, plugin_meta[name], args)
                else:
                    reason = risky_reason(name, args)
                if reason:
                    answer = (await ask(f"The agent wants to {reason}. Allow it? (yes/no)")) if ask else "no"
                    if not answer.strip().lower().startswith("y"):
                        result = f"The user did not allow this action ({reason}). Do not retry it; find another way or call done."
                        log("tool", step=step, name=name, args=args, result=result)
                        messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
                        actions.append(f"{name} -> refused by user")
                        continue

                if loop and focus and name in ("find_on_screen", "look_at_screen", "click_on", "hold_on"):
                    args = {**args, "window": focus}
                if name in ("find_on_screen", "look_at_screen", "click_on", "hold_on") and args.get("window"):
                    hint_focus(str(args["window"]))
                elif name == "App" and args.get("name") and args.get("mode", "launch") in ("launch", "switch"):
                    hint_focus(str(args["name"]))
                elif not loop:
                    hint_focus("")  # working somewhere else: the glow follows the foreground window
                point = None
                if loop and focus and name in ("Click", "hold", "Scroll", "Move", "Drag"):
                    loc = args.get("loc") or []
                    try:
                        point = (int(float(loc[0])), int(float(loc[1])))
                    except (TypeError, ValueError, IndexError):
                        point = None
                blocked = ""
                if point:
                    area = await asyncio.to_thread(content_rect, focus)
                    if area and not (area[0] <= point[0] < area[2] and area[1] <= point[1] < area[3]):
                        blocked = (f"error: ({point[0]}, {point[1]}) is outside the {focus} content area {area}; nothing was clicked. "
                                   "Get the point from find_on_screen.")
                    elif name in ("Click", "hold") and not any(abs(point[0] - x) <= 40 and abs(point[1] - y) <= 40 for x, y in found_points):
                        blocked = "error: nothing was clicked. Don't guess coordinates: call find_on_screen for what you want, then use its x, y."
                if blocked:
                    result = blocked
                elif name in ("find_on_screen", "click_on", "hold_on"):
                    got = await asyncio.to_thread(eyes.find, args.get("description", ""), int(args.get("display", 0) or 0), str(args.get("window") or ""))
                    if "x" in got:
                        pt = (int(got["x"]), int(got["y"]))
                        if focus and (area := await asyncio.to_thread(content_rect, focus)) and not (area[0] <= pt[0] < area[2] and area[1] <= pt[1] < area[3]):
                            got = {"error": f"could not locate it inside the {focus} window"}
                    if "x" not in got:
                        result = json.dumps(got) if name == "find_on_screen" else f"error: {got.get('error', 'could not locate it')}; nothing was clicked"
                    else:
                        same_spot = sum(1 for x, y in found_points[-3:] if abs(pt[0] - x) <= 15 and abs(pt[1] - y) <= 15)
                        found_points[:] = (found_points + [pt])[-8:]
                        if name == "find_on_screen":
                            result = json.dumps(got)
                        elif name == "click_on":
                            result = text_of(await win.call_tool("Click", {"loc": list(pt)})) + f" (found at {pt[0]}, {pt[1]})"
                        else:
                            result = await asyncio.to_thread(hold_mouse, pt[0], pt[1], args.get("seconds", 2))
                        if same_spot >= 2:
                            result += (" Note: this is the same spot your last searches found. If acting on it didn't do what you wanted, "
                                       "it isn't the thing you're after: look at the screen and try something else, or research how this part works.")
                elif name == "look_at_screen":
                    result = await asyncio.to_thread(eyes.describe, args.get("question", ""), int(args.get("display", 0) or 0), str(args.get("window") or ""))
                elif name == "browser_read" and not browser_tab_open:
                    result = "error: IO has no browser tab open in this task. Open a page with browser_open first."
                elif name == "browser_read":
                    try:
                        raw = text_of(await sessions["browser_navigate"].call_tool("browser_evaluate", {"function": "() => document.body.innerText"}))
                        result = raw if raw.startswith("error") else find_in_text(page_text(raw), str(args.get("find") or ""))
                        if not raw.startswith("error"):
                            result = (await meanings_hint(sessions["browser_navigate"], page_text(raw), task)) or result
                    except Exception as e:
                        result = f"error: {e}"
                elif name == "browser_open":
                    # IO's own tab: in Chrome mode the extension connects here and puts the tab in the IO group
                    url = str(args.get("url") or "about:blank")
                    try:
                        result = inline_browser_snapshot(text_of(await sessions["browser_navigate"].call_tool("browser_navigate", {"url": url})), browser_dir)
                        browser_tab_open = not result.startswith("error")
                        if "google." in url and "/search" in url:
                            result = (await search_results(sessions["browser_navigate"])) or result
                        elif DISAMBIGUATION.search(result):
                            result = (await meanings_hint(sessions["browser_navigate"], result, task)) or result
                    except Exception as e:
                        result = f"error: {e}"
                elif name == "ask_gemini" and loop and gemini:
                    result = await consult(str(args.get("question") or "What should I do next?"))
                elif name == "research" and loop:
                    last_research = step
                    try:
                        result = await researcher.ask(str(args.get("question") or task), task)
                        if not result.startswith("error"):
                            guide.append(f"({args.get('question')}) {result}")
                            pin_guide()
                    except Exception as e:
                        result = f"error: research failed: {e}"
                elif name == "wait":
                    secs = max(0.5, min(600.0, float(args.get("seconds") or 1)))
                    await asyncio.sleep(secs)  # cancellable: Stop ends it at once
                    result = f"waited {secs:g}s"
                elif name == "hold":
                    loc = args.get("loc") or [args.get("x"), args.get("y")]
                    try:
                        result = await asyncio.to_thread(hold_mouse, int(float(loc[0])), int(float(loc[1])), args.get("seconds", 2))
                    except (TypeError, ValueError, IndexError):
                        result = "error: hold needs loc=[x, y]"
                elif name == "close_windows":
                    all_but = args.get("all_except")
                    result = await asyncio.to_thread(close_windows, list(args.get("titles") or []), list(all_but) if all_but is not None else None)
                elif name == "remember":
                    result = remember(args.get("note", ""))
                elif name == "ask_user":
                    result = "The user answered: " + ((await ask(args.get("question", ""))) or "(no answer)")
                elif name == "type_text" or (name == "Type" and not args.get("loc")):
                    # Type without a location is the most common small-model slip: type into the focused control instead
                    args = {"text": args.get("text", ""), "press_enter": args.get("press_enter", False)}
                    # paste via clipboard: reliable for any text and keyboard layout
                    await win.call_tool("Clipboard", {"mode": "set", "text": args.get("text", "")})
                    await win.call_tool("Shortcut", {"shortcut": "ctrl+v"})
                    if args.get("press_enter"):
                        await win.call_tool("Shortcut", {"shortcut": "enter"})
                    result = "typed"
                elif (name.startswith("browser_") and name not in ("browser_open", "browser_navigate") and not browser_tab_open):
                    # any browser call connects to Chrome and opens IO's tab group: only once the task has opened a page
                    result = ("error: IO has no browser tab open in this task. browser_* tools only work on web pages opened "
                              "with browser_open; they can't touch windows, dialogs or Chrome's own tabs on the PC. For those use "
                              "Snapshot, Click, find_on_screen or close_windows.")
                elif name in sessions:
                    if name == "browser_navigate":
                        browser_tab_open = True
                    if name not in aliases:
                        args = fix_args(name, args)
                    if name == "Snapshot":
                        # the boss is text-only; skip the screenshot image
                        args = {**args, "use_vision": False, "use_annotation": False}
                    try:
                        # a plugin that stops answering mustn't hang the task (and the queue behind it)
                        timeout = {"read_timeout_seconds": PLUGIN_CALL_TIMEOUT} if name in aliases else {}
                        call_args = {**args, "command": ps_wrap(args["command"])} if name == "PowerShell" and args.get("command") else args
                        result = text_of(await sessions[name].call_tool(aliases.get(name, name), call_args, **timeout))
                        if name == "PowerShell":
                            result = ps_unwrap(result)
                        if name == "App" and args.get("mode") == "switch" and "error" in result.lower() and args.get("name"):
                            # Windows-MCP matches app names exactly; fall back to any window title containing the name
                            if title := await asyncio.to_thread(focus_window, args["name"]):
                                result = f"Switched to {title} window."
                        if name.startswith("browser_"):
                            result = inline_browser_snapshot(result, browser_dir)
                        if name == "App" and args.get("mode") == "launch" and args.get("name") and "launched" in result.lower():
                            # Windows often opens new apps behind the current foreground window
                            await asyncio.sleep(1.0)
                            switched = text_of(await win.call_tool("App", {"mode": "switch", "name": args["name"]}))
                            # a failed switch doesn't mean the launch failed; don't let it read like one
                            result += " " + (switched if "error" not in switched.lower() else "Use Snapshot to find its window.")
                    except Exception as e:  # tool errors go back to the boss to recover from
                        if name in aliases and isinstance(e, MCPError) and e.message == "Connection closed":
                            # the plugin process died: stop offering its tools
                            dead = sessions[name]
                            gone = {a for a in aliases if sessions.get(a) is dead}
                            tools[:] = [d for d in tools if d["function"]["name"] not in gone]
                            for a in gone:
                                sessions.pop(a, None)
                            result = f"error: the plugin behind {name} stopped; its tools are no longer available, do this another way"
                        else:
                            result = f"error: {e}"
                else:
                    result = f"error: there is no tool named {name}"
                result = result[:tool_cap] or "ok"
                last_error = result if re.match(r"(error|\d+ validation error)", result, re.I) or "Error calling tool" in result else ""
                if not last_error:
                    refused_done = False
                error_streak = error_streak + 1 if last_error else 0
                if director and last_error:
                    director_queue.clear()  # the rest of its plan assumed this worked: ask the director again with the result
                actions.append(f"{name}({json.dumps(args, ensure_ascii=False)[:120]}) -> {result[:120]}")  # for replanning
                steps_log.append(f"{name}({json.dumps(args, ensure_ascii=False)[:200]}) -> {result[:1500]}")
                log("tool", step=step, name=name, args=args, secs=round(time.time() - t1, 1), result=result[:300])
                messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
                if name in ("look_at_screen", "PowerShell", "browser_read") and not last_error:
                    last_info = result

        log("gave_up", steps=max_steps)
        return f"stopped after {max_steps} steps without finishing"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task", help="what to do, in plain language")
    parser.add_argument("--max-steps", type=int, default=30)
    args = parser.parse_args()
    print(asyncio.run(run(args.task, args.max_steps)))


if __name__ == "__main__":
    main()
