"""IO's agent loop: a tool-calling model drives Windows through Windows-MCP and IO's action library.

Muse Glimmer 30B (local) or NVIDIA's models (by effort level, see EFFORT) decide; EvoCUA-8B is the "eyes": when
something can't be found in the accessibility tree (unnamed icons, canvases, images), find_on_screen asks it where to
click, and look_at_screen asks it what is on screen.

Usage:  python boss.py "open notepad and type hello"
"""
import argparse
import asyncio
import base64
import contextvars
import ctypes
import dataclasses
import ctypes.wintypes as wt
import hashlib
import io
import itertools
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
import urllib.request
import uuid
from contextlib import AsyncExitStack
from types import SimpleNamespace
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError
from openai import BadRequestError, OpenAI
from PIL import ImageGrab

import actions
import learned
import nim
import planner
import plugins

# physical pixels everywhere, matching Windows-MCP's virtual-desktop coordinates
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

HERE = Path(__file__).parent
BOSS_URL = os.environ.get("BOSS_URL", "http://127.0.0.1:8090/v1")
BOSS_MODEL = os.environ.get("BOSS_MODEL", "boss")
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
# the eyes' screenshots are sized to at most 4096 tiles of 28x28 px (what llama.cpp keeps of a Qwen-VL image anyway)
EYES_MAX_PIXELS = 4096 * 28 * 28
KEEP_FULL_SNAPSHOTS = 2
# replanning: after this many failed calls in a row, or steps without finishing, at most MAX_REPLANS times
REPLAN_AFTER_ERRORS = 2
REPLAN_AFTER_STEPS = 10
MAX_REPLANS = 3
MAX_TOOL_TEXT = 14000
# re-reading the same thing with nothing in between is a loop; repeating an action (undo x3, PageDown, Next) is not
LOOP_PRONE = {"Snapshot", "browser_snapshot", "browser_read", "look_at_screen", "find_on_screen", "click_on", "Scrape", "browser_open", "browser_navigate",
              "read_window", "read_page", "list_controls", "check_screen"}
DESKTOP_READS = {"Snapshot", "look_at_screen", "find_on_screen", "read_window", "list_controls", "check_screen"}  # a window, not a web page
# conditional offers ("If you want, I'll check the weekend too") aren't promises to keep working
OFFER = re.compile(r"[^.!?\n]*\b(if you(?:'d)? (?:want|like|need|prefer)|let me know|would you like|want me to|shall i|should i)\b[^.!?\n]*[.!?]?", re.I)


def intent_text(t: str) -> str:
    return OFFER.sub("", (t or "").replace("\u2019", "'")).lower()


# work it says is still ahead: any verb, not a list ("Next, I will create a folder..." ended a task with only its first
# part done, because "create" wasn't on the old list); offers ("I can also...") are stripped by intent_text first
MORE_TO_DO = re.compile(r"\b(i will|i'll|let me|i am going to|i'm going to|next,? i)\s+(now\s+|then\s+|also\s+)?[a-z]{3,}")
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

# With the action layer (actions.py) the tool part of SYSTEM is generated from the task's menu, so the prompt never names a
# tool the model doesn't have; HOW TO DECIDE and RULES stay. data/actions.json {"enabled": false} brings SYSTEM back.
SYSTEM_LAYER = """You are IO, the user's assistant on their Windows PC. You can chat, answer questions, and do things on the PC with the tools provided.

HOW TO DECIDE
- Chatting (hello, thanks, how are you, what can you do) or a question you can answer from general knowledge: reply in plain text, no tools.
- You are IO. "What is IO?", "who are you" and questions about yourself are about you: answer them yourself, no tools.
- Well-known facts (countries and capitals, famous people and companies, science, history): answer directly, no search.
- A name or word you don't confidently know (a small website, company, product, app, person, slang): {search} before answering
  (always Google, never Bing). Don't guess a similar-sounding word, don't assume it means one of your tools, and don't ask the user what it is until you've searched.
- Needs live or personal information (the time, files, what's open or on screen, a web page, weather, prices): get it with a tool, then answer.
- Asks you to do something on the PC: do exactly that, nothing extra (no saving, closing or double-checking unless asked).
- Ambiguous, or needs something only the user knows (which file, which account): call ask_user instead of guessing.

{pick}

{jobs}RULES
- Results start ok:, unsure: or error:CODE:. After unsure or error, follow its try: hint or check with another action; never report success for a step that failed or wasn't confirmed.
- Never select-all and copy to read text (that replaces the user's clipboard).
- window= is part of a title. Titles change as you work ("Untitled - Notepad" becomes your text), so name the app ("Notepad").
- If something blocked you (a refusal, a user constraint, a failure), say so in done instead of trying around it.
{folders}{browser}- Finish with done. Its summary is your answer to the user: include the actual result (the time, the list, the description, the number), never just "I found it" or "I described it". If something blocked you, say what."""
# (tool the line needs, line): only lines whose tool is on the menu are shown
LAYER_JOBS = [
    ("write_in_app", "Write something in an app: write_in_app(app, text) (it opens the app itself: no open_app first). Save only "
                     "when asked (save_to= with the full path)."),
    ("open_app", "Open or switch to an app: open_app(name). Close one: close_window(window)."),
    ("list_windows", "What's open: list_windows, then list the window titles."),
    ("read_window", "Text in a window (Notepad, a dialog, Calculator's display): read_window(window)."),
    ("click", "Click in an app: click(target, window) with the control's visible text; type_into(field, text, window) for a field."),
    ("pc_info", "The time anywhere, disk space, CPU/GPU/RAM, IP: pc_info(topic, place). Installed or running apps: app_info(name), never by opening it."),
    ("list_files", "Files and folders: list_files, find_file, read_file, write_file, file_op. Never open Explorer for that."),
    ("api_lookup", "Code against an installed library or SDK: api_lookup(of=project/dll/package, type= or find=) shows its real "
                   "API. Use those names, never ones from memory."),
    ("web_answer", "A fact on the web: web_answer(question), then answer from its excerpts. A given page: read_page(url, find=a few words)."),
    ("web_fill", "Use a web page or web app (fill its form, press its buttons, even one on localhost): browser_open(url) opens it in "
                 "IO's tab in the user's browser, then web_fill({label: value, ...}) for all its fields at once and web_click(text). "
                 "Never click or type into a browser window by screen position or with click/type_into."),
    ("look_at_screen", "What's on screen: look_at_screen, then put the full description in your answer."),
    ("open_settings", "A Windows setting: open_settings(page), then set_control(label, value) or read_window."),
    ("click_on", "Games and emulators (BlueStacks): no controls to read. Use click_on and look_at_screen with window set to the app's "
                 "title. Never press Esc or Back there; close menus with their on-screen X."),
]


FRONTIER_STYLE = """WORKING STYLE
- Something ongoing ("keep an eye on", "every morning", "until it's fixed", "whenever X happens"): make it a standing goal
  with add_goal(objective, every_minutes) instead of trying to finish it now; IO then checks on it by itself.
- A task with several parts: start with todo(items) listing them, and call it again (in the same reply as your next action)
  each time a part is done; the user watches that list. Skip it for one-step tasks.
- Think the whole task through first, then act. Every action you already know you'll need goes in this reply: several tool
  calls at once, or one steps call (they run in order; a failure stops the rest). Each reply costs a slow round trip.
- Change code with edit_file (exact old text -> new), or read_file(path, lines="120-180", anchors=true) then edit_lines with
  those line tags (no need to copy the old text); rewrite a whole file only when most of it changes. A big code file
  read without lines= comes back as an outline with line ranges: then read the part you need.
- Run programs and test suites with run_command(command, folder): the whole output in order and the real exit code.
  To keep the output in a file, add save_to="out.txt" (redirecting and then typing the file reports type's exit code,
  not the program's). Use PowerShell only for PowerShell's own cmdlets.
- Check your work the way the user will use it before calling done: run the tests and read their output, request the
  server's page and its API, look at the result. A command's "Status Code" is the last program's exit code: not 0 means it failed.
- Code against a library, SDK or plugin API installed here (a NuGet or pip package, a game's plugin framework): what you
  remember of its API may be from another version. Look up the installed one with api_lookup(of=the project, dll or
  package, type= or find=) before writing calls to it. When a build says a name doesn't exist, its result shows the
  real API: use those names, never another guess. Logs of any size: read_file(path, tail=200) or find="words".
- What IO itself can do: about_io(topic). Its program files are read-only to you. When the task needs an ability none of
  your tools give (not information, not the user's decision), call propose_tool(name, does, why) once: the user decides
  whether IO builds that tool in its workshop. Then carry on with what you can. Do the same when you had to improvise
  the ability with a one-off script (decoding a file type, talking to a program) and it will come up again: proposing
  it in the same reply as your answer means next time it's a tool, not a puzzle.
- Installing or updating a package or program (pip, npm, winget): install_package(manager, name, why); it asks the user
  first and never touches IO's own Python. Don't install with run_command or PowerShell.
- A server or app that must keep running: start_app(command, folder, port) (it waits until the port answers).
- State facts only from what your tools returned in this task. If a page didn't show something, look somewhere better
  (a site's own API, another page) or say you couldn't find it; never fill the gap from memory.

"""


def layer_system(names: list, browser_where: str = "", frontier: bool = False) -> str:
    """SYSTEM for the action layer: HOW TO DECIDE, the preference ladder limited to this menu, COMMON JOBS and RULES.
    frontier: a large NVIDIA brain, which also gets WORKING STYLE (batching, editing, checking its work)."""
    have = set(names)
    search = ("search the web with web_answer (or web_search, then read_page on the best result)" if "web_answer" in have else
              "look it up with research" if "research" in have else "find out with tools(\"WEB\") and use(...)")
    jobs = [line for tool, line in LAYER_JOBS if tool in have]
    browser = ""
    if have & {"web_answer", "web_search", "read_page", "web_click", "web_fill"}:
        browser = (f"- Web actions work in IO's own browser tab. {browser_where} Never drive Chrome or Edge windows with click or keys. "
                   "look_at_screen can't see that tab: answer about a page from read_page.\n")
    return SYSTEM_LAYER.format(search=search, pick=actions.system_tools_text(names),
                               jobs=(FRONTIER_STYLE if frontier else "") +
                               (("COMMON JOBS\n" + "\n".join(f"- {j}" for j in jobs) + "\n\n") if jobs else ""), browser=browser,
                               folders=user_folders())


MADE_FILE = re.compile(r"\b(?:creat|generat|wr[io]t|sav|made|built|produc|export)\w*\b[^.\n]{0,80}?([\w\-]+\.(?:svg|md|txt|csv|json|py|html?|js|css|png|jpe?g|zip|xlsx|docx|pptx|pdf|log|ya?ml|xml|ini|bat|ps1))\b"
                       r"|([\w\-]+\.(?:svg|md|txt|csv|json|py|html?|js|css|png|jpe?g|zip|xlsx|docx|pptx|pdf|log|ya?ml|xml|ini|bat|ps1))\b[^.\n]{0,30}?\b(?:was|were|has been|have been|is now) (?:creat|generat|written|sav|made|built)\w*", re.I)
NOT_DONE = re.compile(r"\b(couldn'?t|could not|can'?t|cannot|didn'?t|did not|unable|failed|wasn'?t|were not|weren'?t|not yet|instead of|would)\b", re.I)
WRITE_TOOLS = ("write_file", "edit_file", "edit_lines", "run_command", "PowerShell", "file_op", "FileSystem", "screenshot", "start_app", "save_file_as", "write_in_app")


def leaked_call(text: str, names: set):
    """A tool call written as text instead of made: <|python_tag|>{"name": t, "parameters": {...}} (Llama), or a bare
    {"name": ..., "arguments": ...}. A call object when the name is a tool this task has, else None."""
    s = re.sub(r"<\|python_tag\|>|<\|eom_id\|>|<\|eot_id\|>|```(?:json)?", "", text or "").strip()
    if not s.startswith("{"):
        return None
    try:
        data = json.loads(s)
    except ValueError:
        data = loose_json(s)
    if not isinstance(data, dict):
        return None
    name = data.get("name") or data.get("tool")
    args = data.get("parameters", data.get("arguments", data.get("args", {})))
    if name not in names or not isinstance(args, (dict, str)):
        return None
    return SimpleNamespace(id=f"leak-{uuid.uuid4().hex[:8]}", type="function",
                           function=SimpleNamespace(name=name, arguments=args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)))


def claimed_unmade(answer: str, steps_log: list[str]) -> str:
    """A file the answer says was made that no step that can write files ever touched ('' when every claim has a step)."""
    text = answer or ""
    claimed = set()
    for m in MADE_FILE.finditer(text):
        clause = re.split(r"[.;\n]", text[:m.start()])[-1] + m.group(0)  # the sentence up to the claim
        if not NOT_DONE.search(clause):  # "I couldn't create chart.svg" is an honest report, not a claim
            claimed.add(m.group(1) or m.group(2))
    writes = " ".join(s for s in steps_log if s.split("(", 1)[0] in WRITE_TOOLS).lower()
    missing = sorted(f for f in claimed if f.lower() not in writes)
    return f"your answer says {', '.join(missing)} {'was' if len(missing) == 1 else 'were'} made, but no step wrote it." if missing else ""


def kill_by_name_problem(name: str) -> str:
    """Why killing by name is the wrong move when the name matches several processes ('' when it matches one or none).
    A run that wanted to stop its own stray web server (it had the pid from netstat) asked to kill 'python.exe', which
    would also have stopped Unsloth Studio (IO's eyes), IO's Windows-MCP servers and four other servers."""
    try:
        import psutil
        want = name.lower().removesuffix(".exe")
        hits = []
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            if (p.info["name"] or "").lower().removesuffix(".exe") == want:
                hits.append(f"{p.info['pid']}: {' '.join(p.info['cmdline'] or [])[:100]}")
    except Exception:
        return ""
    if len(hits) <= 1:
        return ""
    return (f"error:BLOCKED: {len(hits)} processes are named {name}, and killing by name stops all of them (other apps and IO's own "
            "helpers too). Kill the one you mean by its pid. They are:\n" + "\n".join(hits[:15]))


def user_folders() -> str:
    """Where the user's folders really are: Windows can move Documents and Desktop (OneDrive, or by hand), and a brain
    that guessed C:\\Users\\<name>\\Documents built a project in a folder Explorer doesn't show as Documents."""
    try:
        where = ", ".join(f"{n} is {actions.known_folder(n)}" for n in ("Documents", "Desktop", "Downloads"))
        return f"- The user's folders: {where}. \"My Documents\" means that path; use these full paths.\n"
    except Exception:
        return ""


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
STEPS_TOOL = {"type": "function", "function": {
    "name": "steps",
    "description": ("Do several actions in one turn, in order, e.g. tap three ores then wait: "
                    '[{"tool": "click_on", "args": {"description": "the silver ore at the top"}}, '
                    '{"tool": "click_on", "args": {"description": "the copper ore"}}, {"tool": "wait", "args": {"seconds": 2}}]. '
                    f"At most {actions.STEPS_MAX}. Each step gets its own result; the rest are skipped if one fails or the screen "
                    "changes a lot (a popup or new menu). Use it whenever you already know the next few actions."),
    "parameters": actions.tool_schema("steps")["function"]["parameters"]}}
EXTRA_TOOLS.append(STEPS_TOOL)

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


def find_window(title: str, own: bool = False) -> tuple[int, tuple[int, int, int, int]] | None:
    """The visible top-level window whose title contains `title` (case-insensitive): (hwnd, rect). 'hwnd:N' names one
    window exactly. A browser window showing IO's own page (its Duck.ai chat, named after the task) only with own=True."""
    user32 = ctypes.windll.user32
    found: list = []
    m = re.fullmatch(r"hwnd[:=](\d+)", title or "")
    if m:
        hwnd = int(m.group(1))
        r = wt.RECT()
        if user32.IsWindow(hwnd) and user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd) and user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return hwnd, (r.left, r.top, r.right, r.bottom)
        return None

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if title.lower() in buf.value.lower() and (own or not actions.own_page(buf.value)):
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


def display_titles(display: int = 0, limit: int = 10) -> str:
    """The titles of the app windows on one display, front first, so a vision answer can't invent a different desktop."""
    try:
        l, t, r, b = displays()[display]
    except IndexError:
        return ""
    user32, out = ctypes.windll.user32, []
    for hwnd, title in open_windows():
        rc = wt.RECT()
        if user32.IsIconic(hwnd) or not user32.GetWindowRect(hwnd, ctypes.byref(rc)):
            continue
        w = actions._w(hwnd)
        if w is None or w.exe in actions.PROTECTED or actions.own_page(title):
            continue  # the user's private apps (Claude, Discord) are never named in a question that may reach Duck.ai
        cx, cy = (rc.left + rc.right) // 2, (rc.top + rc.bottom) // 2
        if l <= cx < r and t <= cy < b and rc.right - rc.left > 100:
            out.append(f"'{title[:60]}'")
    return ", ".join(out[:limit])


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
    home = wt.POINT()
    user32.GetCursorPos(ctypes.byref(home))
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # left down
    try:
        time.sleep(seconds)
    finally:
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # left up, even if the task is stopped mid-hold
        user32.SetCursorPos(home.x, home.y)  # your pointer goes back where you left it
    return f"held the mouse at ({int(x)}, {int(y)}) for {seconds:g}s"


def quick_click(x: int, y: int) -> str:
    """A click that borrows the pointer for ~130 ms and puts it back where you had it, so working alongside IO is bearable.
    The button stays down for 80 ms: emulators (BlueStacks) poll touch input and drop a 20 ms press."""
    user32 = ctypes.windll.user32
    home = wt.POINT()
    user32.GetCursorPos(ctypes.byref(home))
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.03)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.08)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.01)
    user32.SetCursorPos(home.x, home.y)
    return f"Single left clicked at ({int(x)},{int(y)})."


def window_on_top_at(title: str, pt: tuple[int, int]) -> bool:
    """Whether a click at pt would land in the window whose title contains `title` (not in one covering it)."""
    hit = find_window(title)
    if not hit:
        return True
    user32 = ctypes.windll.user32
    under = user32.WindowFromPoint(wt.POINT(*pt))
    return user32.GetAncestor(under, 2) == hit[0]  # GA_ROOT


def send_to_back(title: str) -> None:
    """Puts the window whose title contains `title` at the bottom of the stack, without minimizing or moving it."""
    if hit := find_window(title, own=True):
        ctypes.windll.user32.SetWindowPos(hit[0], 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # HWND_BOTTOM, no move/size/activate


def input_nudge() -> None:
    """Windows only lets the process that sent the last input hand over the foreground. A tap of an unassigned virtual
    key (0x97) counts as that input and nothing reacts to it. The Alt this used to be landed its key-up in the window
    being focused, which put Win11 Notepad into key-tip mode: the next letters typed were eaten ("hello bench" saved as
    "bench")."""
    user32 = ctypes.windll.user32
    user32.keybd_event(0x97, 0, 0, 0)
    user32.keybd_event(0x97, 0, 2, 0)


def window_behind(hwnd: int, back_to: int = 0) -> None:
    """Puts a window at the bottom of the stack (not minimized) and hands the foreground back to back_to."""
    user32 = ctypes.windll.user32
    user32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # HWND_BOTTOM, no move/size/activate
    if back_to and back_to != hwnd and user32.IsWindow(back_to) and user32.IsWindowVisible(back_to):
        input_nudge()
        user32.SetForegroundWindow(back_to)


def focus_window(title: str) -> str:
    """Brings the window whose title contains `title` to the front (fallback when App switch fails)."""
    hit = find_window(title)
    if not hit:
        return ""
    user32 = ctypes.windll.user32
    if user32.IsIconic(hit[0]):
        user32.ShowWindow(hit[0], 9)  # SW_RESTORE (only when minimized: it would un-maximize a maximized window)
    if user32.GetForegroundWindow() != hit[0]:
        input_nudge()  # Windows only lets the foreground app hand over focus
        user32.SetForegroundWindow(hit[0])
    user32.BringWindowToTop(hit[0])
    user32.SetWindowPos(hit[0], 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)  # HWND_TOP, no move/size, show
    time.sleep(0.15)
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


def brain_view(window: str, content: bool = False) -> str:
    """A screenshot of the window the task works in, as a data URL for the brain's own turn ('' when it can't be had).
    Never focuses anything: it shows what is on screen there now."""
    try:
        rect = (content_rect(window) if content else None) or (find_window(window) or (0, None))[1]
        if not rect or rect[2] - rect[0] < 50 or rect[3] - rect[1] < 50:
            return ""
        shot = ImageGrab.grab(bbox=rect, all_screens=True)
        shot.thumbnail((1280, 1280))
        buf = io.BytesIO()
        shot.convert("RGB").save(buf, format="JPEG", quality=80)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


VIEW_NOTE = ("Screenshot of the '{w}' window, taken just now, after your last actions. Decide from what you see; you don't "
             "need look_at_screen for this window unless you want a closer look. When you're sure what the next few actions "
             "are (for example click_on, wait, click_on), put them all in this reply: they run in order, and you get a new "
             "screenshot after them.")
BRAIN_SUMMARY_SYSTEM = """You compress the working notes of an AI agent on a Windows PC so it can keep going. Summarize the
earlier steps below in at most 500 words: what has been done and what it achieved, what was found (exact values, names,
positions of buttons, menus and items, coordinates, paths, URLs), what worked, and what failed or wasted time. Output
only the summary."""
LEARN_EVERY = 25  # loops: the brain updates its playbook for the task after this many more actions

# EvoCUA always thinks before it answers; llama-server's per-request cap is the one switch it honours (its template has
# no on/off: enable_thinking did nothing). 192 tokens leaves its clicks as accurate as no cap (measured on a Save dialog).
EVO_THINK = {"thinking_budget_tokens": 192}
EVO_THINK_LONG = {"thinking_budget_tokens": 384}  # describing a screen or a picture
QWEN_POINT_PROMPT = ("Find this on the screenshot: {target}\nAnswer only with JSON like {{\"point_2d\": [x, y]}}, the centre "
                     "of it, with x and y on a 0-1000 scale across the image's width and height.")

# The eyes: EvoCUA-8B (Meituan's computer-use model) on its own llama-server. It's asked the way it was
# trained (its "S2" prompt: one computer_use tool call, coordinates on a 1000x1000 grid), with only a click allowed.
EVO_URL = os.environ.get("EVO_URL", "http://127.0.0.1:8091/v1")
EVO_MODEL = "eyes"
EVO_TOOL = {"type": "function", "function": {
    "name_for_human": "computer_use", "name": "computer_use",
    "description": ("Use a mouse and keyboard to interact with a computer, and take screenshots.\n* This is an interface to a "
                    "desktop GUI.\n* The screen's resolution is 1000x1000.\n* Make sure to click any buttons, links, icons, etc "
                    "with the cursor tip in the center of the element. Don't click boxes on their edges unless asked."),
    "parameters": {"properties": {
        "action": {"description": "* `left_click`: Click the left mouse button at a specified (x, y) pixel coordinate on the screen.\n"
                                  "* `terminate`: Terminate the current task and report its completion status.",
                   "enum": ["left_click", "terminate"], "type": "string"},
        "coordinate": {"description": "The x,y coordinates for mouse actions.", "type": "array"},
        "status": {"description": "The status of the task.", "type": "string", "enum": ["success", "failure"]}},
        "required": ["action"], "type": "object"},
    "args_format": "Format the arguments as a JSON object."}}
EVO_SYSTEM = ("# Tools\n\nYou may call one or more functions to assist with the user query.\n\nYou are provided with function "
              "signatures within <tools></tools> XML tags:\n<tools>\n" + json.dumps(EVO_TOOL) + "\n</tools>\n\nFor each function "
              "call, return a json object with function name and arguments within <tool_call></tool_call> XML tags:\n<tool_call>\n"
              "{\"name\": <function-name>, \"arguments\": <args-json-object>}\n</tool_call>\n\n# Response format\n\nResponse format "
              "for every step:\n1) Action: a short imperative describing what to do in the UI.\n2) A single <tool_call>...</tool_call> "
              "block containing only the JSON: {\"name\": <function-name>, \"arguments\": <args-json-object>}.\n\nRules:\n- Output "
              "exactly in the order: Action, <tool_call>.\n- Be brief: one sentence for Action.\n- Do not output anything else "
              "outside those parts.\n- If finishing, use action=terminate in the tool call.")
EVO_ASK = ("\nPlease generate the next move according to the UI screenshot, instruction and previous actions.\n\n"
           "Instruction: Click on {target}. If it isn't on the screen, terminate with status failure.\n\nPrevious actions:\nNone")


def evo_point(client, url: str, target: str) -> tuple[float, float] | None:
    """EvoCUA's click for `target` on one image, as fractions of its width and height; None when it says it isn't there.
    Asked plainly first (its Qwen3-VL base's point_2d answer: on a 4K desktop it hit 7 of 10 described targets, as many
    as UI-TARS, against 6 with its own agent prompt), and with that agent prompt only when the plain answer has no point."""
    plain = local_create(client, EVO_MODEL, _purpose="find where to click", temperature=0, max_tokens=400, extra_body=EVO_THINK, messages=[
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}, {"type": "text", "text": QWEN_POINT_PROMPT.format(target=target)}]},
    ]).choices[0].message.content or ""
    m = re.search(r"\[\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\]", re.sub(r"<think>.*?</think>", "", plain, flags=re.S))
    if m and 0 <= float(m.group(1)) <= 1000 and 0 <= float(m.group(2)) <= 1000:
        return float(m.group(1)) / 1000, float(m.group(2)) / 1000
    reply = local_create(client, EVO_MODEL, _purpose="find where to click (agent prompt)", temperature=0.01, max_tokens=400, extra_body=EVO_THINK, messages=[
        {"role": "system", "content": EVO_SYSTEM},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}, {"type": "text", "text": EVO_ASK.format(target=target)}]},
    ]).choices[0].message.content or ""
    m = re.search(r"\"coordinate\"\s*:\s*\[\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\]", reply)
    if not m or "terminate" in reply.split("<tool_call>")[-1]:
        return None
    x, y = float(m.group(1)), float(m.group(2))
    return (x / 999, y / 999) if 0 <= x <= 999 and 0 <= y <= 999 else None


class Eyes:
    """Where to click and what's on screen: EvoCUA-8B (Meituan's computer-use model) on its own llama-server finds things
    and describes screens for Muse Glimmer, which reads text only. look_at_screen asks the NVIDIA brain's vision models
    first when the task runs on them (High and Max)."""

    def __init__(self, mode: str = "") -> None:
        self.mode = "evo"  # one local stack now; the argument is kept for older callers
        self.client, self.model = OpenAI(base_url=EVO_URL, api_key="local", max_retries=2, timeout=120), EVO_MODEL
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
        frac = evo_point(self.client, url, description)
        if frac is None:
            return {"error": "could not locate it"}
        return {"x": round(left + frac[0] * (right - left)), "y": round(top + frac[1] * (bottom - top))}

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
        text = f"This is a screenshot of {('the ' + window + ' window') if window else f'display {display}'}. {question or 'Describe what is on the screen.'}"
        messages = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}, {"type": "text", "text": text}]}]
        # a frontier model wrote a whole "screen inventory" for a one-button menu (and took minutes in the queue doing it)
        quick = [{"role": "user", "content": [messages[0]["content"][0], {"type": "text", "text": text + (
            " Answer the question directly in at most 120 words: what is open, the exact text of the buttons that matter "
            "and where they are. No full inventory unless asked.")}]}]
        remote = getattr(self, "remote", None) or []
        fresh = [r for r in remote if time.time() - nim._health.get(r[1], {}).get("failed", 0) > 120]  # just failed: last
        for client, model in fresh + [r for r in remote if r not in fresh]:  # the NVIDIA brain's vision models, in turn
            t_req = time.time()
            try:
                reply = nim.create(client, _purpose="look at the screen", model=model, temperature=0.2, max_tokens=1500, messages=quick, timeout=90,
                                   **nim.reasoning(model, "low"))  # a look is reading, not puzzling: it once spent all 700 tokens thinking
                answer = re.sub(r"<think>.*?</think>", "", reply.choices[0].message.content or "", flags=re.S).strip()
                if answer:
                    nim.note(model, time.time() - t_req, True)
                    return answer
                nim.note(model, time.time() - t_req, False)
            except Exception as e:  # next vision model, then the local one
                nim.note(model, time.time() - t_req, False, gone=getattr(e, "status_code", 0) == 404)
        reply = local_create(self.client, self.model,
            _purpose="look at the screen", temperature=0.2, max_tokens=500 if self.brief else 1100,
            extra_body=EVO_THINK if self.brief else EVO_THINK_LONG, messages=messages)
        return re.sub(r"<think>.*?</think>", "", reply.choices[0].message.content or "", flags=re.S).strip() or "(no answer)"


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


# a page listing meanings ("Mercury may refer to:"), not an article whose hatnote points at one ("For other uses, see NYC
# (disambiguation)", which every big Wikipedia article has: it made Tokyo and New York read as lists of meanings)
DISAMBIGUATION = re.compile(r"\b(may|can) (also )?refer to\b|topics referred to by the same term", re.I)
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

    def __init__(self, stack: AsyncExitStack, use_gemini: bool = False) -> None:
        self.stack, self.session = stack, None
        self.gemini = Gemini(stack, "", private=True) if use_gemini else None

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
        if self.gemini:
            # with a web AI allowed, signed-out Gemini (its own hidden throwaway browser, which can search Google itself)
            # answers better than reading two pages locally; the Google route stays as the fallback
            try:
                answer = await self.gemini.ask(RESEARCH_GEMINI.format(task=task, question=question))
                answer = re.sub(r"\n(Sources?|Show all)\b.*", "", answer, flags=re.S).strip()
                # its source chips ("Reddit", "+ 1") come through as short lines of their own
                answer = "\n".join(l for l in answer.splitlines()
                                   if not re.fullmatch(r"\s*\+ ?\d+\s*", l) and not (l.strip() and len(l) < 60 and not re.search(r"[.:!?]", l)))
                if answer and not answer.startswith("error"):
                    return answer[:1500]
                log("warning", text=f"Gemini research failed, using Google: {answer[:120]}")
            except Exception as e:
                log("warning", text=f"Gemini research failed, using Google: {e}"[:200])
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


# ---------- Ultracode: a plan first, then sub-agents doing its parts in parallel (NVIDIA brain only) ----------

ULTRA_MAX_SUBTASKS = 8
ULTRA_MAX_AGENTS = nim.MAX_PARALLEL  # sub-agents working at once (the user's cap: 5); NVIDIA requests are capped the same
ULTRA_HELPER_SECS = 240  # a helper's whole budget; the main agent takes over its part with what it found
ULTRA_CALLS_PER_TURN = 3  # tool calls a helper may make per reply; the rest are dropped (GLM once sent 54 searches at once)
ANNOUNCING = re.compile(r"^\s*(i'?ll|i will|let me|i'?m going to|i am going to|first,? i|next,? i|now,? i)\b", re.I)
# what a sub-agent may run: its own hidden browser and read-only file and PC facts; never the mouse, keyboard or windows
ULTRA_SUB_TOOLS = ["web_search", "read_page", "list_files", "find_file", "read_file", "pc_info", "app_info", "calc", "api_lookup", "about_io"]
ULTRA_PLAN = """You plan for IO, an AI agent on a Windows PC, in Ultracode mode: helpers work on separate parts of a request
at the same time, then the main agent finishes it.
Split the user's request into subtasks. Each has a kind:
- "parallel": needs no mouse, keyboard or windows: Google searches, reading web pages, finding or reading files, facts
  about the PC, maths. Helpers do these at the same time, each with its own hidden browser.
- "desktop": opens, clicks, types into or changes apps, windows, settings or files. The main agent does these afterwards,
  one at a time, with the helpers' results.
Make each parallel subtask specific and self-contained (one item to look up, one source to read, one folder to check),
finishable in a few searches or reads. Use 2-6 parallel subtasks when the request has separate parts (several things to
look up, compare or gather). "deps" lists the ids whose results a subtask needs first (keep it empty when it can).
If nothing in the request is worth doing in parallel, return a single subtask.
Reply ONLY with JSON: {"subtasks": [{"id": "s1", "goal": "...", "kind": "parallel", "deps": []}]}"""
ULTRA_SUB_SYSTEM = """You are a helper sub-agent of IO, an AI agent on a Windows PC. You do ONE subtask of a bigger request
while other helpers do theirs. You have a hidden browser of your own (web_search, read_page) and can read files; you
can't use the mouse, keyboard or windows and can't ask the user. Work quickly: a few calls, then done(summary) with the
facts you found (names, numbers, dates, file paths, page addresses), complete enough that the main agent can use them
without redoing your work. If Google asks for a check, don't search again: read_page a source you know instead (the
official site, or https://en.wikipedia.org/wiki/<Topic>). If part of it needs the desktop or the user's OK, say so in done.
For a library, SDK or plugin API installed on this PC, api_lookup(of=its project, dll or package) reads the real API of
the installed version: trust it over web docs and examples, which are often for another version."""
ULTRA_DONE = {"type": "function", "function": {
    "name": "done", "description": "Finish your subtask with what you found.",
    "parameters": {"type": "object", "properties": {"summary": {"type": "string", "description": "The facts found, with sources"}},
                   "required": ["summary"]}}}


HELPER_PROFILES = HERE / "data" / "helper-profiles"  # one per helper slot (0-4): two browsers can't share a profile
PROFILE_CACHES = ("Cache", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache", "GraphiteDawnCache", "DawnCache",
                  "Crashpad", "Service Worker", "BrowserMetrics*", "*.pma", "Singleton*", "lockfile", "*.lock")


def helper_profile(slot: int) -> Path:
    """Helper slot's browser profile, first made as a copy of the researcher's (its Google cookies and consent): a brand
    new profile gets Google's "unusual traffic" check on its first search."""
    dest = HELPER_PROFILES / str(slot)
    if not dest.exists():
        src = HERE / "data" / "research-profile"
        try:
            if src.exists():
                shutil.copytree(src, dest, ignore=shutil.ignore_patterns(*PROFILE_CACHES), ignore_dangling_symlinks=True,
                                dirs_exist_ok=True, copy_function=lambda a, b: shutil.copy2(a, b) if os.path.exists(a) else None)
        except (OSError, shutil.Error) as e:  # files the researcher has open: whatever copied is enough
            log("warning", text=f"helper profile {slot}: copied partly ({type(e).__name__})")
        dest.mkdir(parents=True, exist_ok=True)
    return dest


def strict_json(text: str) -> bool:
    """Whether a tool call's arguments are a whole JSON object (NVIDIA refuses a conversation holding a broken one)."""
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False


class SubBrowser:
    """A helper's own headless browser (Playwright MCP on its slot's profile), started on its first web call and closed
    with the helper's own exit stack (entered and left in the helper's task)."""

    def __init__(self, stack: AsyncExitStack, slot: int) -> None:
        self.stack, self.slot, self.session, self.lock = stack, slot, None, asyncio.Lock()

    async def call_tool(self, name: str, args: dict):
        async with self.lock:
            if self.session is None:
                profile = await asyncio.to_thread(helper_profile, self.slot)
                params = StdioServerParameters(command="node", args=[str(BROWSER_CLI), "--headless", "--browser", "msedge",
                                                                     "--user-data-dir", str(profile),
                                                                     "--output-dir", str(HERE / "data" / "research"),
                                                                     "--codegen", "none", "--image-responses", "omit"])
                r, w = await self.stack.enter_async_context(stdio_client(params, errlog=sys.stderr))
                self.session = await self.stack.enter_async_context(ClientSession(r, w))
                await self.session.initialize()
        return await self.session.call_tool(name, args)


def ultra_subtasks(plan: dict | None) -> list[dict]:
    """The planner's subtasks, cleaned: safe unique ids, known deps only, at most ULTRA_MAX_SUBTASKS parallel ones, and a
    parallel one that needs a desktop one, or sits in a dependency loop, becomes desktop (the main agent does it after)."""
    def safe(x) -> str:  # ids reach the panel's HTML: letters, digits, - and _ only
        return re.sub(r"[^A-Za-z0-9_-]", "", str(x))[:24]

    subs, seen, renamed, n_par = [], set(), {}, 0
    raw = (plan or {}).get("subtasks") if isinstance(plan, dict) else None
    for i, s in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(s, dict) or not str(s.get("goal") or "").strip():
            continue
        kind = "desktop" if str(s.get("kind", "")).lower().startswith("desk") else "parallel"
        if kind == "parallel":
            if n_par >= ULTRA_MAX_SUBTASKS:
                continue
            n_par += 1
        base = safe(s.get("id") or "") or f"s{i + 1}"
        sid, k = base, 2
        while sid in seen:
            sid, k = f"{base}_{k}", k + 1
        seen.add(sid)
        renamed.setdefault(safe(s.get("id") or ""), sid)
        deps = s.get("deps") or []
        deps = [deps] if isinstance(deps, (str, int)) else deps if isinstance(deps, list) else []
        subs.append({"id": sid, "goal": str(s["goal"]).strip()[:600], "kind": kind, "deps": [safe(d) for d in deps if isinstance(d, (str, int))]})
    for s in subs:
        s["deps"] = [renamed[d] for d in s["deps"] if d in renamed and renamed[d] != s["id"]]
    kinds = {s["id"]: s["kind"] for s in subs}
    changed = True
    while changed:  # desktop-ness flows down the deps
        changed = False
        for s in subs:
            if s["kind"] == "parallel" and any(kinds[d] == "desktop" for d in s["deps"]):
                s["kind"] = kinds[s["id"]] = "desktop"
                changed = True
    done_ids, order = set(), []
    par = [s for s in subs if s["kind"] == "parallel"]
    while True:  # topological: whatever can never run (a cycle) is left out
        ready = [s for s in par if s["id"] not in done_ids and all(d in done_ids for d in s["deps"])]
        if not ready:
            break
        for s in ready:
            done_ids.add(s["id"])
            order.append(s)
    for s in par:  # caught in a dependency loop: the main agent does it, after the helpers
        if s["id"] not in done_ids:
            s["kind"], s["deps"] = "desktop", []
    return order + [s for s in subs if s["kind"] == "desktop"]


RESEARCH_GEMINI = """Research for an AI agent that is doing this on a PC: {task}
Question: {question}
Search the web if useful. Answer in at most 150 words: concrete steps, the names of buttons, menus and items, what to do
first and what to avoid. No preamble."""
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

{goal}
{shot}
{screen}Recent actions and their results, oldest first:
{history}

Tools:
{catalog}

Reply with ONLY a JSON object, no other text:
{{"thoughts": "<one sentence: what you see and why these actions>", "actions": [{{"tool": "<tool name>", "args": {{...}}}}]}}
Give 1 to 8 actions to do next, in order. Use wait when the game needs time. After they run you'll get the results and a new
screenshot. If an action fails, the rest are skipped and you're asked again."""


# Duck.ai's "Customize responses" holds these as its standing instructions, so each conversation starts with just the
# current state. It reads only about the first 500 characters, so it carries the role and the reply contract; the tool
# list comes with the first message (it differs per task).
DIRECTOR_STANDING = ("You direct IO, an agent operating a Windows PC; a small local model executes your actions exactly. Reply ONLY "
                     'with JSON: {"thoughts":"one sentence","plan":"your current long-term plan, short","actions":[{"tool":"<name>",'
                     '"args":{...}}]}, 1-8 actions from the tools '
                     "IO lists. Describe click targets by look and position. Use wait when things need time. When a task is done, "
                     "call done with the answer. If something keeps failing, change approach.")
DIRECTOR_BRIEF = """IO: {goal}
{shot}{plan}What guides say about it (for long-term planning):
{guide}

{screen}Recent actions and results, oldest first:
{history}

Tools:
{catalog}
Next JSON."""
# the action layer's brief: the nested catalog (top level plus the route's groups expanded) instead of a flat tool list,
# the resolved request and the user's constraints. Budget (Duck.ai, 4,400 chars) in fit_director_prompt's cut order.
DIRECTOR_BRIEF_LAYER = """IO: {goal}
{constraints}{hint}{shot}{plan}{guide}{screen}Recent actions and results, oldest first:
{history}

{catalog}
{expansions}
Next JSON."""
# expect= is checked literally (a window title or text in the window), so it has to be what will be on screen, not a
# description of success ("The text appears in Notepad" failed a step that worked)
DIRECTOR_STANDING_LAYER = actions.DIRECTOR_STANDING.replace('"expect":"what you should see"', '"expect":"a window title or exact text that will show"')
# when Duck.ai stopped answering in JSON, the standing rules go inline too
DIRECTOR_PROMPT_LAYER = DIRECTOR_STANDING_LAYER.replace("{", "{{").replace("}", "}}") + "\n\n" + DIRECTOR_BRIEF_LAYER
DESCRIBED = re.compile(r"\b(is|are|was|appears?|opens?|opened|shows?|showing|visible|focused|ready|displayed|should|will|now|success\w*|"
                       r"saved|typed|written|created|closed|maximi[sz]ed|minimi[sz]ed|selected|entered|done|complete\w*|in front|frontmost)\b|'s\b", re.I)


def usable_expect(expect: str) -> str:
    """The part of a director's expect= the action library can check: quoted text, else short literal text or a title.
    A description of success ('Notepad is open and focused') is dropped: the action's own check covers it, and checked
    literally it would fail a step that worked."""
    # quotes, not apostrophes: "Notepad's title shows 'eggs'" quotes eggs, not "s title shows "
    quoted = re.findall(r"\"([^\"]{2,80})\"|“([^”]{2,80})”|(?<!\w)['‘]([^'’]{2,80})['’](?!\w)", expect or "")
    if quoted:
        return next(q for q in quoted[0] if q)
    e = re.sub(r"\s+(dialog|window|box|popup|page|tab|screen)\W*$", "", (expect or "").strip(), flags=re.I)  # "Save As dialog": title "Save As"
    return "" if not e or DESCRIBED.search(e) or len(e.split()) > 6 else e
DIRECTOR_BUDGETS = {"history": 1600, "guide": 1400, "screen": 700, "expansions": 1800, "goal": 2400}  # cut in this order...
DIRECTOR_FLOORS = {"history": 1100, "guide": 200, "screen": 300, "expansions": 900, "goal": 700}  # ...down to these first
# A director with no web chat's length limit (GLM over NVIDIA's API) gets a roomier brief: every group expanded (a tools()
# round costs it ~30 s) and longer results. Still capped: a smaller request queues and answers faster.
DIRECTOR_ROOMY_CHARS = 16000
DIRECTOR_BUDGETS_ROOMY = {"history": 6000, "guide": 3000, "screen": 1500, "expansions": 9000, "goal": 6000}
# ...and, with no 500-character standing limit, the full rules once per task as its system message. Its rounds are slow
# (12-44 s on the free endpoint, an action takes 1-3 s), so it is told to batch: each round saved is ~30 s.
DIRECTOR_BATCHING = """Speed: each of your replies takes about 30 seconds, an action 1-3 seconds. So give every action you can already
predict in one reply, up to 8, in order (e.g. open_app, type_into, hotkeys, then read_window to check). End the batch early
only where you must see a result before you can choose (search results, a list to pick from, a dialog you can't predict).
Each action checks itself; when one fails or is unsure the rest are skipped and you get the results.
When the batch's own checks will prove the task done and you already know the answer (e.g. calculator("37*24") then done
with "37 Ã— 24 = 888"), end the batch with done(summary). When the answer is something an action reads, wait for its result.
Describe click targets by what they look like and where they are. Never repeat an action that just failed the same way."""
# After a director batch that ended on one of these groups' actions and worked, the local model may call done itself
# (a ~30 s GLM round that only said done cost A4 more than half its time)
FINISH_GROUPS = {"DO", "READ", "PC"}
FINISH_CHECK = """The actions above all worked. The user's request: {task}
If their results already complete the request, call done now with the answer for the user (the real values from the
results). If anything is still left to do, reply with just: not yet."""
TEXT_SLOT = "Â«TEXTÂ»"  # stands in for a long text the user gave (to type or write), which IO puts back into the director's args
TEXT_SLOT_AT = 900  # requests longer than this send their text as the slot


def text_slot(request: str) -> tuple[str, str]:
    """(goal, payload): a long request's text to type moved out of the director's brief (Duck.ai takes 4,400 characters
    in all) and replaced by TEXT_SLOT, which IO fills back into the actions. ('request', '') when it is short or has no
    'instruction: text' shape."""
    if len(request) <= TEXT_SLOT_AT:
        return request, ""
    m = re.search(r"[:\n]", request[:400])
    if not m or len(request) - m.end() < 200:
        return request, ""
    head, payload = request[:m.end()].rstrip(), request[m.end():].strip()
    return (f"{head} {TEXT_SLOT} (the user's text, {len(payload)} characters, starting \"{payload[:100]}\" and ending "
            f"\"{payload[-60:]}\"; write {TEXT_SLOT} in an action's args where it goes, and IO puts the full text there)"), payload


def fill_slot(args, payload: str):
    """TEXT_SLOT in any string argument -> the user's text."""
    if not payload:
        return args
    if isinstance(args, str):
        return args.replace(TEXT_SLOT, payload)
    if isinstance(args, dict):
        return {k: fill_slot(v, payload) for k, v in args.items()}
    if isinstance(args, list):
        return [fill_slot(v, payload) for v in args]
    return args


def tool_catalog(tools: list[dict]) -> str:
    """One line per tool for the director: name, arguments and what it does."""
    lines = []
    for t in tools:
        f = t["function"]
        schema = f.get("parameters", {})
        required, parts = set(schema.get("required", [])), []
        for k, v in schema.get("properties", {}).items():
            if k in ("window", "display") or (k not in required and len(parts) >= 3):  # IO fills those in; rarely needed extras left out
                continue
            enum = v.get("enum") or (v.get("anyOf") or [{}])[0].get("enum")
            parts.append(k + ("" if k in required else "?") + (f"={'|'.join(map(str, enum[:4]))}" if enum else ""))
        params = ", ".join(parts)
        desc = ("finish: summary = the answer for the user (in a loop: a progress note, and IO keeps going)" if f["name"] == "done"
                else (f.get("description") or "").split(". ")[0][:90])
        lines.append(f"- {f['name']}({params}): {desc}")
    return "\n".join(lines)


def fit_director_prompt(limit: int, template: str = DIRECTOR_PROMPT, budgets: dict | None = None, **parts: str) -> str:
    """DIRECTOR_PROMPT under the site's length limit: older guide notes, history and the screen description go first.
    budgets (the action layer's brief): parts are cut one at a time in the dict's order, each down to its DIRECTOR_FLOORS
    entry before the next is touched; expansions lose whole groups from the end."""
    ordered = budgets is not None
    budgets = dict(budgets) if ordered else {"guide": 1400, "history": 1600, "screen": 700}

    def cut(k, v):  # whole lines only: the newest history, the start of the guide notes
        if k == "expansions":  # whole groups, in the route's order
            out, n = [], 0
            for block in v.split("\n\n"):
                if n + len(block) > budgets[k]:
                    break
                out.append(block)
                n += len(block) + 2
            return "\n\n".join(out)
        if k == "goal":  # the start of the task says what to do
            return v if len(v) <= budgets[k] else v[:budgets[k]].rsplit(" ", 1)[0] + " …(cut)"
        lines, out, n = v.splitlines(), [], 0
        for line in (reversed(lines) if k == "history" else lines):
            if n + len(line) > budgets[k]:
                break
            out.append(line)
            n += len(line) + 1
        return "\n".join(reversed(out) if k == "history" else out) or v[:budgets[k]]

    for _ in range(24):
        filled = {k: cut(k, v) if k in budgets else v for k, v in parts.items()}
        prompt = template.format(**{k: v for k, v in filled.items() if "{" + k + "}" in template})
        if len(prompt) <= limit:
            return prompt
        if not ordered:
            for k in budgets:
                budgets[k] = int(budgets[k] * 0.75)
            continue
        over = len(prompt) - limit
        k = next((k for k in budgets if budgets[k] > DIRECTOR_FLOORS.get(k, 0) and parts.get(k)), None)
        if k:
            budgets[k] = max(DIRECTOR_FLOORS.get(k, 0), min(int(budgets[k] * 0.75), budgets[k] - over))
        else:  # everything at its floor: cut all alike
            for k in budgets:
                budgets[k] = int(budgets[k] * 0.75)
    # never cut the end: the catalog and "Next JSON." are what make the reply usable
    tail = prompt[-600:]
    return prompt[:limit - len(tail) - 2] + "\n…" + tail if len(prompt) > limit else prompt


_JSON_STR = re.compile(r'"((?:[^"\\]|\\.)*)"', re.S)
# a Windows path inside a string: C:\..., %TEMP%\..., ~\..., or a UNC \\server at its start
_HAS_PATH = re.compile(r'(?<![A-Za-z])[A-Za-z]:\\(?!\\)|%\w+%\\(?!\\)|^~\\(?!\\)|^\\\\(?!\\)')


def _escape_paths(s: str) -> str:
    """Single backslashes inside strings that hold a Windows path doubled before parsing: "C:\\temp\\new.txt" written raw
    by a model is valid JSON with a TAB and a newline in it, which no later repair can tell from intent. Properly
    escaped paths ("C:\\\\temp") are left alone."""
    def fix(m: re.Match) -> str:
        body = m.group(1)
        if not _HAS_PATH.search(body):
            return m.group(0)
        return '"' + re.sub(r'\\\\|\\"|\\', lambda t: t.group(0) if len(t.group(0)) == 2 else "\\\\", body) + '"'
    return _JSON_STR.sub(fix, s)


def _salvage_actions(s: str) -> dict | None:
    """A reply cut off mid-JSON (or with one broken action): thoughts, plan and every action object that parses on its
    own, in order. None when no action survives."""
    m = re.search(r'"actions"\s*:\s*\[', s)
    if not m:
        return None
    dec, i, acts = json.JSONDecoder(), m.end(), []
    while i < len(s):
        while i < len(s) and s[i] in " \t\r\n,":
            i += 1
        if i >= len(s) or s[i] != "{":
            break
        try:
            obj, i = dec.raw_decode(s, i)
        except ValueError:
            break
        acts.append(obj)
    if not acts:
        return None
    out: dict = {"actions": acts}
    for k in ("thoughts", "plan"):
        km = re.search(r'"' + k + r'"\s*:\s*"((?:[^"\\]|\\.)*)"', s)
        if km:
            try:
                out[k] = json.loads('"' + km.group(1) + '"')
            except ValueError:
                out[k] = km.group(1)
    return out


def loose_json(text: str) -> dict | None:
    """The JSON object in a model's reply, forgiving the usual slips: Windows paths with single backslashes
    ("C:\\Temp", repaired before parsing so \\t and \\n in them stay path characters), trailing commas, a stray
    language tag ("json\\n{...") and a reply cut off after some complete actions. None when there is no object."""
    start = (text or "").find("{")
    if start < 0:
        return None
    end = text.rfind("}")
    s = text[start:end + 1] if end > start else text[start:]
    escaped = re.sub(r'\\(?![\\"/bfnrtu])', r'\\\\', _escape_paths(s))
    for candidate in (_escape_paths(s), escaped, re.sub(r",\s*([}\]])", r"\1", escaped), s):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        return data if isinstance(data, dict) else None
    return _salvage_actions(escaped) or _salvage_actions(_escape_paths(text[start:]))


def split_steps(calls: list) -> tuple[list, dict]:
    """A steps(...) call becomes its actions, as if the model had made them as parallel tool calls: each gets its own
    result message (the chat format needs one per call id) and goes through every check a single call does.
    Returns (calls, batch_of): batch_of maps each new call id to (the steps call's id, its position, a note for its result).
    A steps call that can't be read stays as it is, and its result says why."""
    out, batch_of = [], {}
    for c in calls:
        if c.function.name != "steps":
            out.append(c)
            continue
        try:
            args = json.loads(c.function.arguments or "{}")
        except ValueError:
            args = loose_json(c.function.arguments or "")
        got, note = actions.expand_steps(args.get("steps") if isinstance(args, dict) else None)
        if not got:
            out.append(c)
            continue
        for i, (tool, a) in enumerate(got):
            cid = f"{c.id}-{i}"
            out.append(SimpleNamespace(id=cid, function=SimpleNamespace(name=tool, arguments=json.dumps(a, ensure_ascii=False))))
            batch_of[cid] = (c.id, i, note if i == 0 else "")
    if len(out) > 1:
        # several calls in one reply are a batch too: a click that missed made the typing after it land in the wrong
        # place (IO's own window); only a failed action stops the rest, since independent reads don't depend on each other
        rest = [c for c in out if c.id not in batch_of]
        for i, c in enumerate(rest):
            batch_of[c.id] = ("reply:" + rest[0].id, i, "")
    return out, batch_of


# reads that touch nothing shared (no window, no browser tab, no model slot): several in one reply run at once
PARALLEL_SAFE = {"read_file", "list_files", "find_file", "pc_info", "app_info", "calc", "api_lookup"}

# calls that only look (a failed one doesn't stop the other calls in its reply)
READ_ONLY = {"web_search", "web_answer", "read_page", "read_file", "list_files", "find_file", "look_at_screen", "Snapshot",
             "list_windows", "list_controls", "find_control", "read_window", "check_screen", "find_on_screen", "browser_snapshot",
             "browser_read", "research", "ask_model", "wait", "pc_info", "app_info", "game_state", "todo", "notes"}


def director_plan_of(text: str, code: str = "") -> str:
    """The long-term plan the director keeps in its replies ('' if none)."""
    data = (loose_json(code) if code else None) or loose_json(text) or {}
    plan = data.get("plan") or ""
    return (plan if isinstance(plan, str) else json.dumps(plan, ensure_ascii=False))[:600]


def parse_director(text: str, allowed: set, code: str = "") -> tuple[str, list[tuple[str, dict]]]:
    """(thoughts, [(tool, args)]) from the director's reply; tools IO doesn't have are dropped. code: the reply's code
    block as the page holds it (tried first: the visible text can carry the fence and the Copy button). An action's
    "expect" goes into its args; the action library checks it, everything else drops it."""
    data = (loose_json(code) if code else None) or loose_json(text)
    if not data:
        return "", []
    acts = data.get("actions") or data.get("action") or []  # it sometimes answers with a single "action"
    out = []
    for a in acts if isinstance(acts, list) else [acts]:
        if isinstance(a, str):
            a = {"tool": a, "args": {k: v for k, v in data.items() if k not in ("thoughts", "action", "actions", "plan")}}
        name = a.get("tool") or a.get("name") if isinstance(a, dict) else None
        if name in allowed:
            args = a.get("args") or a.get("arguments") or a.get("parameters") or a.get("input")
            if not isinstance(args, dict):  # or the arguments sit next to the tool name
                used = "tool" if a.get("tool") else "name"  # "name" can be an argument (App's) when "tool" names the tool
                args = {k: v for k, v in a.items() if k not in (used, "args", "arguments", "parameters", "input", "expect")}
            if a.get("expect") and isinstance(a.get("expect"), str):
                args = {**args, "expect": a["expect"]}
            out.append((name, args))
    return str(data.get("thoughts") or "")[:400], out[:8]


class Gemini:
    """Opt-in: asks Gemini for advice, a fresh chat each time, at most once every GEMINI_EVERY seconds.
    private: a throwaway signed-out browser (hidden, in-memory profile, Gemini's default model) that is closed after
    each question, so nothing is saved anywhere. Otherwise: the user's own Chrome (IO's tab group) and account."""

    conversational, in_browser = False, True  # a fresh chat per question

    def __init__(self, stack: AsyncExitStack, token: str, private: bool = True) -> None:
        self.stack, self.token, self.session, self.last, self.private = stack, token, None, 0.0, private
        self.tab = False  # account mode: IO's tab is open in Chrome (a navigate worked)
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
            # registered after the session, so it runs before the session closes (the stack unwinds last-in first-out)
            self.stack.push_async_callback(self.close)
        return self.session

    async def close(self) -> None:
        """Account mode, end of the task (done, Stop or an error): hand IO's tab back; the extension closes it on DONE_URL."""
        if self.tab and not self.private:  # (with no tab, any call would make the extension open a new connect page)
            await hand_back_tab(self.session)
        self.tab = False

    def ready_in(self) -> int:
        return max(0, round(self.last + GEMINI_EVERY - time.time()))

    async def _ask(self, prompt: str, image: bytes = b"") -> str:
        if wait := self.ready_in():
            return f"error: Gemini was asked recently; it can be asked again in {wait}s. Keep going with what you have."
        self.last = time.time()
        s = await self._session()
        self.tab, had_tab = True, self.tab  # (should Stop land mid-navigate, the tab may well be open)
        self.tab = had_tab or not text_of(await s.call_tool("browser_navigate", {"url": GEMINI_URL})).startswith("error")
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
# Where IO's Chrome tabs go when a task ends: IO's extension closes any tab that arrives here, even the last tab of a
# window, which it otherwise only ungroups and leaves open (Duck.ai chat and all). Never mid-task: closing the last
# controlled tab ends the extension's connection, and with it that session's browser tools.
DONE_URL = f"http://127.0.0.1:{os.environ.get('BOSS_APP_PORT', '8765')}/static/io-done.html"


async def hand_back_tab(session, before: str = "") -> None:
    """End of a task: run `before` in IO's tab (deleting the Duck.ai chat), then send the tab to DONE_URL.
    Never raises; shielded, so a second Stop during the cleanup can't leave the tab half done."""
    async def goodbye():
        try:
            if before:
                await asyncio.wait_for(session.call_tool("browser_evaluate", {"function": before}), 4)
            await asyncio.wait_for(session.call_tool("browser_navigate", {"url": DONE_URL}), 5)
        except (Exception, asyncio.CancelledError):  # the tab may be gone already, or closed by the extension mid-navigation
            pass
    try:
        await asyncio.shield(goodbye())
    except asyncio.CancelledError:
        pass


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
  const asked = main.split('You said').length - 1;
  const text = parts.length > 1 ? parts[parts.length - 1] : '';
  // the reply's JSON as its code block holds it (the visible text carries the fence label and Copy button); only when
  // that block belongs to the newest answer, not an older one
  const blocks = [...document.querySelectorAll('pre code')];
  const fallback = blocks.length ? blocks : [...document.querySelectorAll('pre, code')];
  const last = fallback.length ? fallback[fallback.length - 1].textContent : '';
  const flat = s => s.replace(/\\s+/g, '');
  const code = last && text && last.includes('{') && flat(text).includes(flat(last).slice(0, 60)) ? last : '';
  return JSON.stringify({limited, challenge, asked, generating: /Generating response/.test(main), text, code});
}"""
# Duck.ai keeps recent chats in the browser: delete just this one (its own Delete chat button), nothing else of yours
DUCK_MAX_IMAGES = 5
# Duck.ai's daily cap (~250 messages here): once it says so, every task skips it for a while instead of each spending a
# 30 s round finding out again. Kept in a file so a restart remembers.
DUCK_LIMIT_PAUSE = 3600
DUCK_LIMIT_FILE = HERE / "data" / "duck_limit.json"


def duck_paused_until() -> float:
    try:
        return float(json.loads(DUCK_LIMIT_FILE.read_text(encoding="utf-8")).get("until", 0))
    except (OSError, ValueError, AttributeError):
        return 0.0


def duck_pause(until: float) -> None:
    try:
        DUCK_LIMIT_FILE.write_text(json.dumps({"until": until}), encoding="utf-8")
    except OSError:
        pass
# Duck.ai refuses a 6th picture in a conversation and then won't send anything until the pending ones are removed
DUCK_IMAGE_LIMIT_JS = """() => {
  if (!/only attach \\d+ images? per conversation/i.test(document.body.innerText)) return 'ok';
  [...document.querySelectorAll('button')].filter(b => /^remove image/i.test(b.getAttribute('aria-label') || '')).forEach(b => b.click());
  return 'full';
}"""
# "Customize responses" > Additional instructions, kept beside whatever else you set there (Duck.ai stores it in the browser)
DUCK_INSTRUCT_JS = """() => { let c = {}; try { c = JSON.parse(localStorage.getItem('duckaiCustomization')) || {}; } catch (e) {}
  c.version = c.version || '1'; c.data = Object.assign({}, c.data, {additionalInstructions: %s});
  localStorage.setItem('duckaiCustomization', JSON.stringify(c)); localStorage.setItem('duckaiCustomizationActive', 'true'); return 'ok'; }"""
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


def complete_json(text: str) -> bool:
    return loose_json(text) is not None


def duck_answer(text: str) -> str:
    """The reply out of the page text after 'Duck.ai said': without the model name above it or the app promo below."""
    text = re.split(r"\n(Duck\.ai works best|Jump to latest response|Download\n|Tools\n)", text)[0]
    lines = [l for l in text.strip().splitlines() if l.strip() and l.strip().lower() not in ("2nd opinion", "copy", "retry", "show reasoning")
             and not re.fullmatch(r"\d+s", l.strip())]  # its buttons and the "thought for 1s" label
    if lines and len(lines[0]) < 40 and not lines[0].rstrip().endswith((".", "!", "?")):
        lines = lines[1:]  # the model's name, e.g. "GPT-5.6 Luna"
    return "\n".join(lines).strip()


class DuckAI:
    """Opt-in: asks Duck.ai (DuckDuckGo's private chat, no account) in IO's tab in the user's Chrome, with a screenshot.
    A fresh chat each time, Duck.ai's local chat history wiped afterwards, at most once every GEMINI_EVERY seconds.
    If Duck.ai asks to prove you're human, IO stops: that's for you to do, not IO."""

    private, takes_images, max_chars = True, True, ADVISOR_MAX_CHARS["DuckAI"]
    conversational, in_browser = True, True
    standing_max = 500  # it reads only about this much of its standing instructions: the rest goes in the messages
    max_images = DUCK_MAX_IMAGES  # per conversation: the director starts a new one before the next picture

    def __init__(self, stack: AsyncExitStack, token: str) -> None:
        self.stack, self.token, self.session, self.last = stack, token, None, 0.0

    def ready_in(self) -> int:
        return max(0, round(self.last + GEMINI_EVERY - time.time()))

    async def _session(self):
        if self.session is None:
            r, w = await self.stack.enter_async_context(stdio_client(browser_params({"browser_mode": "chrome", "chrome_token": self.token}), errlog=sys.stderr))
            self.session = await self.stack.enter_async_context(ClientSession(r, w, client_info=BROWSER_CLIENT))
            await self.session.initialize()
            # registered after the session, so it runs before the session closes (the stack unwinds last-in first-out)
            self.stack.push_async_callback(self.close)
        return self.session

    async def close(self) -> None:
        """End of the task (done, Stop or an error): delete the conversation and hand the tab back; the extension closes
        it on DONE_URL. (Left to the disconnect alone, a tab that is the last in its window stayed open, chat and all.)"""
        if self.tab:  # (with no tab, any call would make the extension open a new connect page)
            await hand_back_tab(self.session, DUCK_FORGET_JS)
        self.in_chat, self.images, self.tab = False, 0, False
        actions.OWN_PAGE_TITLES.clear()

    tab = False  # IO's tab is open in Chrome (a navigate worked)
    in_chat = False  # a director conversation is open in the tab
    images = 0  # pictures sent in it (Duck.ai allows DUCK_MAX_IMAGES per conversation)
    last_code = ""  # the last answer's code block as the page holds it (parse_director tries it first)

    async def ask(self, prompt: str, image: bytes = b"", keep: bool = False, instructions: str = "") -> str:
        """keep: continue the open conversation (and leave it open after) instead of a fresh, forgotten chat."""
        if wait := self.ready_in():
            return f"error: the advisor was asked recently; it can be asked again in {wait}s. Keep going with what you have."
        self.last = time.time()
        self.last_code = ""
        s = await self._session()

        async def js(code: str) -> str:
            return page_text(text_of(await s.call_tool("browser_evaluate", {"function": code})))

        # a full conversation can't take the director's next screenshot: that round starts a new one (it sends the full brief)
        if not (keep and self.in_chat) or (image and self.images >= DUCK_MAX_IMAGES):
            if self.in_chat:
                await js(DUCK_FORGET_JS)  # the old conversation goes, like any finished one
                self.in_chat = False
            # a fresh URL each time forces a real reload (duck.ai -> duck.ai kept the old conversation on screen; going via
            # about:blank instead made the extension let go of the tab)
            self.tab, had_tab = True, self.tab  # (should Stop land mid-navigate, the tab may well be open)
            opened = text_of(await s.call_tool("browser_navigate", {"url": f"{DUCK_URL}?r={int(time.time() * 1000)}"}))
            self.tab = had_tab or not opened.startswith("error")
            await asyncio.sleep(1)
            if instructions:  # standing instructions for the new conversation (read when the page loads)
                await js(DUCK_INSTRUCT_JS % json.dumps(instructions))
            await js(DUCK_NO_HISTORY_JS)  # don't keep IO's chats in Duck.ai's history in your browser
            await s.call_tool("browser_navigate", {"url": f"{DUCK_URL}?r={int(time.time() * 1000) + 1}"})
            await asyncio.sleep(3)
            self.images = 0
        self.in_chat = False
        try:  # how many messages the conversation already has: the answer must come after a new one
            before = json.loads(await js(DUCK_READ_JS)).get("asked", 0)
        except ValueError:
            before = 0
        if image and self.images >= DUCK_MAX_IMAGES:
            image = b""  # this conversation is full: text only (the director starts a fresh one next round)
        if image and "no-input" in await js(DUCK_ATTACH_JS % json.dumps(base64.b64encode(image).decode())):
            prompt = prompt.replace("A screenshot of the window I'm working in is attached.\n\n", "")
            image = b""
        await asyncio.sleep(2 if image else 0.3)
        if image and "full" in await js(DUCK_IMAGE_LIMIT_JS):
            # "You can only attach 5 images per conversation": take the picture back off, or nothing can be sent
            image, self.images = b"", DUCK_MAX_IMAGES
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
            for _ in range(120):
                await asyncio.sleep(1)
                try:
                    state = json.loads(await js(DUCK_READ_JS))
                except ValueError:
                    continue
                if state.get("limited"):
                    return "error: limit: Duck.ai says its usage limit is reached"
                if state.get("challenge"):
                    return "error: Duck.ai asked to prove a human is there; that's for the user, not IO"
                if state.get("asked", 0) <= before:
                    if _ > 15:  # the message never went out (e.g. the conversation's picture limit): don't reuse the old answer
                        text = ""
                        break
                    continue
                new = duck_answer(state.get("text", ""))
                same = same + 1 if new and new == text and not state.get("generating") else 0
                text, code = new, str(state.get("code") or "")
                if same >= 2 or (same >= 1 and (complete_json(text) or complete_json(code))):  # a finished JSON reply needn't wait for a second check
                    self.last_code = code
                    break
        finally:
            if text and not asyncio.current_task().cancelling():
                try:  # Duck.ai names the chat after the task ("Open document window"): its window is IO's, never the user's
                    title = (await asyncio.wait_for(js("() => document.title"), 3)).strip()
                    if title and not title.startswith("###") and len(title) < 120:
                        actions.OWN_PAGE_TITLES.add(title)
                except Exception:
                    pass
            if keep and text:
                self.in_chat = True
                self.images += bool(image)
            elif not asyncio.current_task().cancelling():  # on Stop, close() deletes the chat and hands the tab back
                try:
                    await js(DUCK_FORGET_JS)
                    # a fresh, empty Duck.ai page: about:blank made the extension let go of the tab, which then stayed behind
                    await s.call_tool("browser_navigate", {"url": f"{DUCK_URL}?r={int(time.time() * 1000)}"})
                except Exception:
                    pass
        # (whole: 8 actions with their expect= run past 2,000 characters, and a cut reply lost its last actions)
        return text[:8000] if text else "error: no answer from Duck.ai"


# How long a failed link sits out before the chain asks it again. GLM's 429 is its 40-a-minute cap (a minute clears it);
# anything else (unreachable, a timeout, an empty answer) costs a whole round of up to 4 minutes, so it waits longer.
CHAIN_RETRY = {"limit": 60, "other": 300}
CHAIN_FOR_RUN = ("no NVIDIA API key", "prove a human")  # nothing a retry fixes this task: skipped until the next one


class DirectorChain:
    """The director as a fallback chain behind the interface IO asks of one (ask, takes_images, max_chars, images, in_chat,
    ready_in, close, conversational): GLM-5.3 Flash on NVIDIA first and Duck.ai behind it (or the other way round), and
    when every link fails the caller's local model decides. The link that answered stays the active one for the run;
    `via` names it, so the logs say who decided. Duck.ai's daily-limit pause (data/duck_limit.json) holds for its link."""

    private = True

    def __init__(self, links: list) -> None:
        self.links, self.active, self.via, self.last_code, self.model = links, 0, type(links[0]).__name__, "", ""
        self.down: dict[int, float] = {}  # link -> when it may be asked again
        self.in_browser = False  # the last round went through a browser tab (IO puts that window back behind)

    @property
    def link(self):
        return self.links[self.active]

    takes_images = property(lambda self: self.link.takes_images)
    max_chars = property(lambda self: self.link.max_chars)
    images = property(lambda self: getattr(self.link, "images", 0))
    in_chat = property(lambda self: self.link.in_chat)
    conversational = property(lambda self: getattr(self.link, "conversational", False))

    def _free_at(self, i: int) -> float:
        paused = duck_paused_until() if isinstance(self.links[i], DuckAI) else 0.0
        return max(self.down.get(i, 0.0), paused)

    def paused_until(self) -> float:
        """0 while some link can be asked; else when the first one can be again (inf: none this run)."""
        soonest = min(self._free_at(i) for i in range(len(self.links)))
        return 0.0 if soonest <= time.time() else soonest

    def ready_in(self) -> int:
        until = self.paused_until()
        return 0 if not until else 86400 if until == math.inf else max(0, round(until - time.time()))

    async def ask(self, prompt, image: bytes = b"", keep: bool = False, instructions: str = "") -> str:
        """prompt: the message, or a function link -> (message, standing rules), since the links differ in length limit,
        standing rules and whether their conversation is open (a link taking over mid-task needs the full brief)."""
        errors, limited, self.in_browser, self.model = [], True, False, ""
        for i in [self.active] + [i for i in range(len(self.links)) if i != self.active]:
            if self._free_at(i) > time.time():
                errors.append(f"{type(self.links[i]).__name__}: paused")
                continue
            link, name = self.links[i], type(self.links[i]).__name__
            text, rules = prompt(link) if callable(prompt) else (prompt[-link.max_chars:], instructions)
            self.in_browser = self.in_browser or getattr(link, "in_browser", False)
            try:
                reply = await (link.ask(text, image, keep=True, instructions=rules) if keep and getattr(link, "conversational", False)
                               else link.ask(text, image))
            except Exception as e:  # a link that raises is a failed link, not a failed task
                reply = f"error: {type(e).__name__}: {e}"
            self.via = name
            if not reply.startswith("error"):
                if i != self.active:
                    log("warning", text=f"the director is now {name} ({'; '.join(errors)[:300]})")
                self.active, self.last_code = i, getattr(link, "last_code", "")
                self.model = link.name() if callable(getattr(link, "name", None)) else ""  # NVIDIA: which catalog model
                return reply
            reply = nim.scrub(reply)  # an API error never carries the key into a log
            errors.append(f"{name}: {reply[7:200]}")
            link.in_chat = False  # its conversation missed this round: a fresh brief if it is asked again
            limited = limited and reply.startswith("error: limit")
            if reply.startswith("error: limit"):
                if isinstance(link, DuckAI):
                    duck_pause(time.time() + DUCK_LIMIT_PAUSE)  # every task skips it for a while, as without the chain
                else:
                    self.down[i] = time.time() + CHAIN_RETRY["limit"]
            else:
                self.down[i] = math.inf if any(w in reply for w in CHAIN_FOR_RUN) else time.time() + CHAIN_RETRY["other"]
        if all(e.endswith(": paused") for e in errors):
            return "error: limit: every director is paused (" + ", ".join(type(l).__name__ for l in self.links) + ")"
        return ("error: limit: " if limited else "error: ") + "; ".join(errors)

    async def close(self) -> None:
        for link in self.links:
            await link.close()


def make_director(stack: AsyncExitStack, options: dict):
    """The stronger AI the settings ask for, or None (the local model decides alone). gemini_mode "nim": GLM-5.3 Flash
    (NVIDIA) and Duck.ai as a chain in director_order; Duck.ai joins only with IO's Chrome token."""
    how, token = options.get("gemini_mode", "private"), options.get("chrome_token", "")
    if not options.get("ask_gemini"):
        return None
    if how == "nim":
        links = [nim.NimDirector(), DuckAI(stack, token) if token else None]
        if options.get("director_order") == "duck_first":
            links.reverse()
        return DirectorChain([l for l in links if l])
    if how in ("account", "duck") and not token:
        return None
    return DuckAI(stack, token) if how == "duck" else Gemini(stack, token, how != "account")


async def meanings_hint(session, page: str, task: str) -> str:
    """On a page that lists different meanings of a word, show the links that fit the task, so the agent opens the right one."""
    if not DISAMBIGUATION.search(page[:1500]):  # the top of the page: the hatnote and lead, not the article body
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
    elif isinstance(loc, list) and len(loc) >= 2:  # ["812", "640"] from the compact schemas
        try:
            args["loc"] = [round(float(loc[0])), round(float(loc[1]))]
        except (TypeError, ValueError):
            pass
    return args


def match_schema(args: dict, tool_def: dict | None) -> dict:
    """Arguments converted to the types the tool's schema asks for: a model typed 12.5 into browser_type's text and got a
    validation error, costing a round trip, twice in one run."""
    props = (((tool_def or {}).get("function") or {}).get("parameters") or {}).get("properties") or {}
    out = dict(args)
    for k, v in args.items():
        want = (props.get(k) or {}).get("type")
        if want == "string" and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = str(v)
        elif want in ("number", "integer") and isinstance(v, str) and re.fullmatch(r"\s*-?\d+(\.\d+)?\s*", v):
            out[k] = int(float(v)) if want == "integer" else float(v)
        elif want == "boolean" and isinstance(v, str) and v.strip().lower() in ("true", "false"):
            out[k] = v.strip().lower() == "true"
    return out


ARTIFACTS = HERE / "data" / "artifacts"
ARTIFACT_DAYS = 7  # spilled results older than this are deleted when a task starts
_spilled: dict[str, str] = {}  # a result's sha1 -> its artifact id (saved once, however often compact() runs)


def spill(content: str) -> str:
    """Saves a whole tool result to data/artifacts/<id>.txt (once) and returns its id: a result cut to fit the model's
    memory stays readable in full with read_file("artifact://<id>", lines=... or find=...), instead of being lost."""
    key = hashlib.sha1(content.encode("utf-8", "replace")).hexdigest()
    if key not in _spilled:
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        (ARTIFACTS / f"{key[:12]}.txt").write_text(content, encoding="utf-8", newline="")  # byte for byte, as returned
        _spilled[key] = key[:12]
    return _spilled[key]


def prune_artifacts() -> None:
    try:
        cutoff = time.time() - ARTIFACT_DAYS * 86400
        for f in ARTIFACTS.glob("*.txt"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except OSError:
        pass


def cut_with_link(content: str, keep: int, why: str) -> str:
    """The start and end of a long result, with a link to all of it (the end of a command's output is usually where
    the error or the total is)."""
    try:
        link = f"artifact://{spill(content)}"
    except OSError:  # the disk said no: cut as before
        return content[:keep] + f"\n[{why}; call the tool again for the rest]"
    tail = min(400, keep // 4)
    lines = content.count("\n") + 1
    return (content[:keep - tail] + f"\n[... {len(content) - keep:,} characters left out ...]\n" + content[-tail:] +
            f"\n[{why}. Full result ({len(content):,} characters, {lines:,} lines): {link}. Read parts of it with "
            f"read_file(\"{link}\", lines=\"1-200\") or find=\"words\".]")


def compact(messages: list[dict], keep: int = 2, trim: int = 1500, snaps_kept: int = KEEP_FULL_SNAPSHOTS, cap: int = 0) -> list[dict]:
    """Keep only the newest tool results in full. Older Snapshots are dropped (huge and stale); other
    older results (web pages, documents, plugin output) are cut short so the context doesn't overflow, each with a link
    to its whole text (spill), so nothing is lost.
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
            m = {**m, "content": cut_with_link(m["content"], trim, "older result trimmed")}
        elif cap and m["role"] == "tool" and len(m["content"]) > cap:
            m = {**m, "content": cut_with_link(m["content"], cap, "result cut to fit the model's memory")}
        out.append(m)
    return out


# Windows PowerShell 5.1 writes its output in the ANSI code page, so names like "Battlefieldâ„¢ 6" came back garbled or the
# line went missing, and its first-run progress records arrived as CLIXML noise. The command's output is passed back as
# base64 UTF-8 instead, with progress records off, and files are read as UTF-8 (5.1 assumes ANSI).
# Errors come back as their plain text, the way a terminal shows them: PowerShell 5.1 wraps each line a program writes to
# stderr in an error record ("python : Traceback..." plus five lines of "At line:2 char:39 / + CategoryInfo ..."), which
# buried a failing test run's traceback. The last program's exit code comes back too (IOEC): Windows-MCP's "Status Code"
# is PowerShell's own, always 0, so a crashed test run read as a success and the brain moved on.
PS_WRAP = ("$ProgressPreference = 'SilentlyContinue'; $PSDefaultParameterValues['*:Encoding'] = 'utf8'; $global:LASTEXITCODE = $null; "
           "$env:PYTHONUNBUFFERED = '1'; "  # a Python program's prints and its traceback come back in the order they happened
           "$__io = & {{\n{cmd}\n}} 2>&1 | ForEach-Object {{ if ($_ -is [System.Management.Automation.ErrorRecord]) {{ $_.Exception.Message }} "
           "else {{ $_ }} }} | Out-String -Width 300; $__ec = $global:LASTEXITCODE; "
           "'IO64:' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($__io)) + ' IOEC:' + $__ec")


def ps_wrap(cmd: str) -> str:
    return PS_WRAP.format(cmd=cmd)


LAUNCHES = re.compile(r"(?i)\bStart-Process\b|\bsaps\b|(^|[;&|]\s*)start\s+(?!-)")  # commands that start something else


def ps_here(wrapped: str, timeout: float = 30) -> str:
    """A PowerShell command run by IO itself (not inside Windows-MCP's job), answered in Windows-MCP's format so
    ps_unwrap reads it the same way. What it starts outlives the task."""
    encoded = base64.b64encode(wrapped.encode("utf-16le")).decode("ascii")
    args = ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    flags = 0x08000000  # CREATE_NO_WINDOW
    try:
        try:
            r = subprocess.run(args, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, cwd=os.path.expanduser("~"),
                               creationflags=flags | 0x01000000)  # CREATE_BREAKAWAY_FROM_JOB, in case IO itself is in one
        except OSError:
            r = subprocess.run(args, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, cwd=os.path.expanduser("~"),
                               creationflags=flags)
    except subprocess.TimeoutExpired:
        return "Response: Command execution timed out\n\nStatus Code: 1"
    out = (r.stdout or r.stderr or b"").decode("utf-8", errors="replace")
    return f"Response: {out}\n\nStatus Code: {r.returncode}"


def ps_unwrap(result: str) -> str:
    m = re.search(r"IO64:([A-Za-z0-9+/=]*)", result)
    if not m:
        return result  # e.g. the command didn't parse: PowerShell's own error is the answer
    try:
        text = base64.b64decode(m.group(1)).decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
    except ValueError:
        return result
    status = re.search(r"Status Code: *(-?\d+)", result)
    code = re.search(r"IOEC:(-?\d+)", result)  # the last program's exit code, when the command ran one
    code = code.group(1) if code else (status.group(1) if status else "0")
    failed = " (the last program failed)" if code not in ("0", "") else ""
    return f"Response: {text or '(no output)'}\n\nStatus Code: {code}{failed}"


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


focus_hwnd = 0  # the same window, once resolved (exe-aware, never IO's own Duck.ai window); 0 = not known


def hint_focus(title: str) -> None:
    """Where the task works now. Resolved once to a window the way actions.resolve does it (by exe for app words, IO's
    own browser pages skipped), so the glow and the refocus after a Duck.ai round can't pick the Duck.ai chat that
    Duck.ai names after the task ("Notepad task execution")."""
    global focus_hint, focus_hwnd
    focus_hint = title
    focus_hwnd = 0
    if title:
        try:
            w = actions.resolve(None, title)
            focus_hwnd = w.hwnd if w and not (w.exe in actions.BROWSERS and not re.search(r"chrome|edge|browser|firefox", title, re.I)) else 0
        except Exception:
            focus_hwnd = 0


def glow_hint() -> str:
    """What the focus glow outlines: 'hwnd:N' once the window is known, else the title hint."""
    if focus_hwnd and ctypes.windll.user32.IsWindow(focus_hwnd):
        return f"hwnd:{focus_hwnd}"
    return focus_hint


def refocus(title: str) -> str:
    """Brings the task's window back to the front after a Duck.ai round: by hwnd when known, else by title (browser
    windows showing IO's own pages skipped)."""
    if focus_hwnd and focus_hint and title == focus_hint and ctypes.windll.user32.IsWindow(focus_hwnd):
        return actions._text(focus_hwnd) if actions._focus_sync(focus_hwnd) else ""
    return focus_window(title)


# the Ultracode sub-agent a record comes from ({"id", "label"}; None = the main agent). A context variable, so records
# logged from worker threads (asyncio.to_thread copies the context) and from a sub-agent's tools carry the tag too
AGENT: contextvars.ContextVar = contextvars.ContextVar("agent", default=None)
_log_lock = threading.Lock()  # sub-agents log at the same time, some from worker threads


# How hard IO works on a task (Settings > Effort, or the message's own pick). One local stack at every level: Muse
# Glimmer 30B thinks, EvoCUA-8B sees and clicks. The levels differ in who decides and how much each model reasons:
#   api      False: nothing leaves the PC; "hard": the local model decides, but tasks the route table marks as needing
#            the stronger brain (driving apps, open-ended work) start on NVIDIA's, and a local run that keeps failing
#            hands over to it; True: NVIDIA's models decide every step
#   local    Glimmer's reasoning for its own steps (its template's "Reasoning strength"; measured on one puzzle: low ~380
#            thinking tokens, medium ~490, high ~1300, all right). Its one-line helpers always run with thinking off.
#   api_reason  what the NVIDIA models are asked for (nim.reasoning maps it to each model's own switch)
#   steps    the most steps a task gets (Settings' "most steps" caps it)
#   out      Glimmer's answer budget per step: reasoning counts against it
#   check    a second look at the work before the answer, on the local model (the NVIDIA brain checks itself too)
#   ultracode  the brain may split the task among helper sub-agents
EFFORT = {
    "low": {"api": False, "local": "low", "api_reason": "low", "steps": 30, "out": 1500, "check": False, "ultracode": False},
    "medium": {"api": "hard", "local": "medium", "api_reason": "medium", "steps": 60, "out": 2500, "check": False, "ultracode": False},
    "high": {"api": True, "local": "high", "api_reason": "high", "steps": 120, "out": 4096, "check": True, "ultracode": False},
    "max": {"api": True, "local": "high", "api_reason": "max", "steps": 200, "out": 4096, "check": True, "ultracode": True},
}
# extra output tokens for a request whose model thinks first (its thoughts count against max_tokens)
REASON_ROOM = {"low": 0, "medium": 1500, "high": 3000, "max": 6000}
EFFORT_NOW: contextvars.ContextVar = contextvars.ContextVar("effort", default="high")  # the running task's level
MEDIUM_LOCAL_REPLANS = 1  # Medium: the local model gets one new plan when stuck; stuck again, the NVIDIA brain takes over


class Escalate(Exception):
    """Medium: the local model is stuck; run() starts the task again on the NVIDIA brain with what was done so far."""


def escalation_in(err: BaseException) -> "Escalate | None":
    """The Escalate inside an error, however deep the task groups nested it, else None."""
    if isinstance(err, Escalate):
        return err
    if isinstance(err, BaseExceptionGroup):
        for sub in err.exceptions:
            if (found := escalation_in(sub)) is not None:
                return found
    return None


BOSS_SEES = [False]  # Muse Glimmer loads without its vision part (room for EvoCUA): pictures reach it as EvoCUA's words
# NVIDIA model id (a part of it) -> what the local model's file name contains when this PC runs the same model
LOCAL_TWINS = {"muse-glimmer-30b": "glimmer"}
LOCAL_TWIN_CHARS = 75_000  # what fits the local copy's 32K-token context with room for its answer; larger goes to NVIDIA
LOCAL_TWIN_OUT = 4096
_ban: dict = {}


def glimmer_off() -> dict:
    """Glimmer with thinking off. Its template has no on/off switch (enable_thinking did nothing, and no reasoning budget
    is enforced for it): it starts thinking by addressing itself (" to=self"), so banning that token sends it straight
    to its answer or tool call. Measured: 0 thinking tokens, answers and tool calls still right, and the prompt cache is
    untouched (an effort change would make it re-read the tools and history)."""
    if "id" not in _ban:
        tid = 19669  # "=self" in Glimmer's vocabulary: [328 " to", 19669 "=self"]
        try:
            req = urllib.request.Request(BOSS_URL.removesuffix("/v1") + "/tokenize", data=json.dumps({"content": " to=self"}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                toks = json.loads(r.read()).get("tokens") or []
            if len(toks) == 2:
                tid = int(toks[1])
        except Exception:
            pass
        _ban["id"] = tid
    return {"logit_bias": {str(_ban["id"]): False}}


def local_reasoning(level: str) -> dict:
    """llama-server fields for Glimmer at a reasoning level: off, or its template's low / medium / high."""
    return glimmer_off() if level == "off" else {"reasoning_effort": level}


def boss_file() -> str:
    """The file name of the model on the local boss server ('' when it isn't up)."""
    try:
        with urllib.request.urlopen(BOSS_URL.removesuffix("/v1") + "/props", timeout=3) as r:
            return Path(str(json.loads(r.read()).get("model_path", ""))).name.lower()
    except Exception:
        return ""


_described: dict = {}  # picture (its hash) -> EvoCUA's description, so an image kept in the conversation is described once


def words_for_pictures(messages: list) -> list:
    """The conversation for a local brain that reads text only: each picture becomes EvoCUA's description of it."""
    out = []
    for m in messages:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list) and any(p.get("type") == "image_url" for p in c):
            parts = []
            for p in c:
                if p.get("type") != "image_url":
                    parts.append(p)
                    continue
                url = p["image_url"]["url"]
                key = hashlib.sha1(url.encode()).hexdigest()
                if key not in _described:
                    try:
                        r = local_create(OpenAI(base_url=EVO_URL, api_key="local", max_retries=1, timeout=120), EVO_MODEL,
                                         _purpose="describe a picture for the brain", temperature=0.2, max_tokens=1100, extra_body=EVO_THINK_LONG,
                                         messages=[{"role": "user", "content": [p, {"type": "text", "text": (
                                             "Describe this image for someone who can't see it: what it shows, every piece of text "
                                             "that matters (exactly as written), and where the main buttons and items are.")}]}])
                        _described[key] = re.sub(r"<think>.*?</think>", "", r.choices[0].message.content or "", flags=re.S).strip()
                    except Exception as e:
                        _described[key] = f"(couldn't be described: {e})"[:200]
                parts.append({"type": "text", "text": f"[a picture, described by the eyes model: {_described[key]}]"})
            m = {**m, "content": parts}
        out.append(m)
    return out


REASONING_KEYS = ("reasoning_effort", "logit_bias", "thinking_budget_tokens", "chat_template_kwargs")


def local_create(client, model: str, _purpose: str = "", **kw):
    """A local llama-server call, timed for the debug timeline like NVIDIA's (nim.create). Glimmer gets the running
    task's reasoning level unless the caller set one; a step whose whole budget went to thinking (no answer, no tool
    call) is asked again with thinking off, which costs nothing in prompt cache."""
    glimmer = model == BOSS_MODEL
    if glimmer and not BOSS_SEES[0] and kw.get("messages"):
        kw["messages"] = words_for_pictures(kw["messages"])
    if glimmer:
        extra = dict(kw.get("extra_body") or {})
        if not any(k in extra for k in REASONING_KEYS):
            extra.update(local_reasoning(EFFORT[EFFORT_NOW.get()]["local"]))
        kw["extra_body"] = extra
    t0 = time.time()
    chars, images = nim.size_of(kw.get("messages"))
    extra = kw.get("extra_body") or {}
    level = "off" if "logit_bias" in extra else extra.get("reasoning_effort", "")
    rec = {"model": "local: " + str(model), "purpose": _purpose, "chars": chars, "images": images, "tools": len(kw.get("tools") or []),
           "wait": 0, **({"reasoning": level} if glimmer and level else {})}
    try:
        r = client.chat.completions.create(model=model, **kw)
    except Exception as e:
        nim.trace({**rec, "ok": False, "secs": round(time.time() - t0, 1), "error": f"{type(e).__name__}: {str(e)[:120]}"})
        raise
    u = getattr(r, "usage", None)
    m = r.choices[0].message if r.choices else None
    thought = len(str((getattr(m, "model_extra", None) or {}).get("reasoning_content") or "")) if m is not None else 0
    nim.trace({**rec, "ok": True, "secs": round(time.time() - t0, 1), "in_tokens": getattr(u, "prompt_tokens", None),
               "out_tokens": getattr(u, "completion_tokens", None), "calls": [c.function.name for c in (m.tool_calls or [])] if m else [],
               **({"thought_chars": thought} if thought else {})})
    if (glimmer and m is not None and r.choices[0].finish_reason == "length" and not (m.content or "").strip() and not m.tool_calls
            and "logit_bias" not in kw["extra_body"]):
        kw["extra_body"] = kw["extra_body"] | glimmer_off()
        return local_create(client, model, _purpose=_purpose + " (again, thinking off: the budget went to thinking)", **kw)
    return r


def log(event: str, **data) -> None:
    agent = AGENT.get()
    if agent and agent["stop"].is_set():
        return  # a helper's thread still finishing after Stop: its records would land in the next task
    record = {"t": round(time.time(), 2), "event": event, **({"agent": agent["id"], "agent_label": agent["label"]} if agent else {}), **data}
    line = json.dumps(record, ensure_ascii=False, default=str)
    if nim.nim_key() and nim.nim_key() in line:  # never the NVIDIA key, in the log file or on screen
        line = nim.scrub(line)
        record = json.loads(line)
    with _log_lock:
        print(line[:600], flush=True)
        for listener in listeners:
            listener(record)
        with open(HERE / "logs" / "boss.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")


nim.listeners.append(lambda rec: log("llm", **rec))


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
    # (format alone is format.com; Format-Table/Format-List only lay out output: they asked the user to allow a read)
    r"(?<![-\w])(Remove-Item|rm|del|erase|rmdir|rd|Format-Volume|format(?!-)|Clear-Content|Stop-Computer|Restart-Computer|shutdown|"
    r"Stop-Process|taskkill|kill|Set-ExecutionPolicy|reg\s+delete|Remove-ItemProperty|Uninstall-\w+|Send-MailMessage)(?![-\w])",
    re.I,
)


# installing or updating software from a command line: asked like any install (install_package is the way IO should do it)
RISKY_INSTALL = re.compile(r"(?<![-\w])(pip3?\s+install|python\S*\s+-m\s+pip\s+install|uv\s+(pip\s+install|add|tool\s+install)|"
                           r"npm\s+(i|install|update)|winget\s+(install|upgrade)|choco\s+(install|upgrade)|scoop\s+install|"
                           r"Install-(Package|Module|Script)|Update-Module)(?![-\w])", re.I)


def risky_reason(name: str, args: dict, request: str = "") -> str:
    """Why an action needs confirmation, or "" if it doesn't. request: what the user asked (a named clipboard is theirs)."""
    if name == "PowerShell" and RISKY_INSTALL.search(str(args.get("command", ""))):
        return f"install software: {args.get('command')}"
    if name == "PowerShell" and RISKY_POWERSHELL.search(str(args.get("command", ""))):
        return f"run PowerShell: {args.get('command')}"
    if name == "FileSystem" and (args.get("mode") in ("delete", "move") or (args.get("mode") == "write" and args.get("overwrite"))):
        return f"{args.get('mode')} the file {args.get('path')}"
    if name == "FileSystem" and args.get("mode") == "write":  # writing over a file that is there replaces it
        try:
            if Path(os.path.expandvars(str(args.get("path") or ""))).exists():
                return f"overwrite the file {args.get('path')}"
        except (OSError, ValueError):
            pass
    if name == "Clipboard" and args.get("mode") == "set" and not re.search(r"clipboard|copy", request, re.I):
        return "replace what is on your clipboard"
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


RISK_KINDS = [("delete", re.compile(r"\b(delete|recycle|remove|remove-item|rm|del|erase|rmdir|rd|clear-content|format)\b", re.I)),
              ("overwrite", re.compile(r"\b(overwrite|replace)\b", re.I)), ("kill", re.compile(r"\b(kill|stop-process|taskkill)\b", re.I)),
              ("close", re.compile(r"\b(close|alt\+f4|ctrl\+w)\b", re.I)), ("run", re.compile(r"\b(run|start-process)\b", re.I))]


def risk_kind(reason: str) -> str:
    """What kind of risky action a confirmation was about, so a 'no' covers the same thing done another way (recycle,
    then Remove-Item, then FileSystem delete asked the user four times for one deletion)."""
    return next((k for k, rx in RISK_KINDS if rx.search(reason)), reason)


# plugin tools (notes, databases, git, GitHub...) that change things: by the server's own hint, by name,
# or SQL that writes. Plain reads and SELECTs don't ask, so unattended tasks aren't held up.
PLUGIN_RISKY = re.compile(r"(delete|remove|drop|reset|merge|push|move|rename|overwrite|write|update|edit|commit|create|close|truncate"
                          # smart-home and service tools act on the real world: lights, locks, a server restart
                          r"|action|call_service|restart|restore|reload|toggle|trigger|(^|_)(turn|set|add|lock|unlock|open|activate|run)(_|$))", re.I)
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
    text = re.sub(r"^\s*done\s*:\s*(?=\S)", "", text, flags=re.I)  # or a "done:" label before it
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
Tool results are real readings from this PC: never doubt them from your own knowledge (games, apps, versions and names
newer than you know exist). One result that answers the request is enough, even if other attempts returned nothing.
If the results reasonably support the answer, say YES: don't ask for extra proof the user didn't want.
A step the request leaves to the user ("tell me how to load it", "I'll sign in", a test only they can run on their own
device or game) isn't missing: if the answer tells them how, that part is done.
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
# what a director sees and may use on single tasks: the full list would overflow Duck.ai's 4,500-character messages
DIRECTOR_TOOLS = {"App", "Snapshot", "click_on", "hold_on", "type_text", "Shortcut", "Scroll", "PowerShell", "wait", "look_at_screen",
                  "close_windows", "browser_open", "browser_read", "research", "remember", "done"}
HELPER_SYSTEM = ("An AI agent working on a Windows PC asks you for advice. Its goal: {goal}\nAnswer its question directly in at most "
                 "150 words: what to do next and why, naming buttons and places as they appear on screen. Don't ask questions back.")
# screen points in tool results: list_controls' "(4108,101)", find_on_screen's {"x": 1, "y": 2}, Snapshot's [x, y]
SCREEN_POINT = re.compile(r"\((\d{1,5}),\s*(\d{1,5})\)|\[(?=\d)(\d{1,5}),\s*(\d{1,5})\]|\"x\":\s*(\d{1,5}),\s*\"y\":\s*(\d{1,5})")
# the NVIDIA brain's reply budget: 2,048 cut off a reply that wrote a whole source file in one call (its longest
# reply in the hard test was 2,019 tokens); all five brain models accept this much (tested 2026-10-04)
BRAIN_MAX_OUT = 16384
RACE_TALK_GRACE = 8.0  # seconds a race waits, after a reply without a tool call, for one that has a tool call
REFLECT_NOTE = ("Your last steps didn't work. Take stock before acting: in a few lines, write what you already know for certain "
                "(facts found, folders and files made, what is running or open), then what is left of the task. Don't redo "
                "anything already done. Then, in this same reply, call the tool for the next thing that's left.")
DEAD_TAPS_NUDGE = 3  # taps in a row with no visible effect before the result suggests asking a stronger model
TAP_ACTIONS = {"click_on", "hold_on", "Click", "hold", "click", "swipe"}  # checked for an effect in a loop's window
LOOP_TOOLS = {"steps","click_on", "hold_on", "look_at_screen", "wait", "Scroll", "Shortcut", "type_text", "App", "research", "done"}
# (those two sets are the rollback lists; with the action layer the loop set is actions.LOOP_MENU plus these window actions:
# a covered or minimised game window is brought back with focus_window, not with guessed clicks)
LOOP_WINDOW_ACTIONS = ["focus_window"]
VAGUE_NOTE = ("The request doesn't say which file or window. Look first (list_files/find_file, without opening anything), "
              "then ask_user which one, naming the candidates you found as the choices; if they don't say, open nothing")
# what else a locked loop may reach through use() or the director: looking, clicking found points, notes; never apps,
# files, the web or closing windows
LOOP_LOCK_EXTRA = {"find_on_screen", "find_all", "read_region", "Click", "hold", "remember", "ask_gemini", "tools", "use", "done", "wait"}
REMEMBER_REQUEST = re.compile(r"\bremember\b|\bnote (that|this|down)\b|\bfrom now on\b|\bnext time\b", re.I)
INFO_GROUPS = {"READ", "PC", "FILE", "WEB", "DO"}  # native results that answer something: the fallback when a summary says nothing
GLOW_NEUTRAL = {"PowerShell", "Clipboard", "Process", "FileSystem", "Scrape", "Snapshot", "remember", "wait", "research", "ask_user",
                "ask_gemini", "ask_model", "steps", "todo", "notes", "tools", "use"}  # tools that work in no window: the focus glow stays where it is
LOOP_KEEP_RECENT = 6
LOOP_REPEAT_LIMIT = 25
LOOP_RESEARCH_EVERY = 20  # steps without research before a loop looks up whatever it's working on now  # the same call this many times in a row is a rut, even in a game
LOOP_NOTE = """LOOP MODE (the user approved it): this goal has no end. Keep working toward it, action after action, until the
user presses Stop. You are never finished, so never stop to ask the user anything: decide for yourself.
- Things change while you work: look again (look_at_screen / find_on_screen) before acting on old information.
- Act with click_on and hold_on (they find and act in one step). Never guess coordinates.
- Do more per turn: when you can already see the next few actions (several ores to tap, a menu and then its button, tap
  then wait), send them together in one steps call. It stops by itself if a step fails or a popup appears, and each step's
  result says whether the screen changed.
- Use wait when something needs time (a timer, an animation, resources building up), with the seconds you think it needs.
- Stuck, or don't know how something works? Call research with a question (it reads guides without leaving the app).
- Calling done only records a short progress note (what you did, what changed, what you'll do next); then you carry on.
- If something isn't working, try a different approach instead of repeating it.
- Older steps get folded into a progress summary to save space; the goal above always stays."""


def loop_goal(task: str) -> str:
    return re.sub(r"^\s*/loop\b\s*", "", task).strip() or task


def local_chat(system: str, user: str, max_tokens: int = 300, think: bool | str = True) -> str:
    """One question to Glimmer. think: True = the task's reasoning level, False = none (one-line helpers: thinking used to
    eat their whole budget and leave no answer), or a level ("low")."""
    client = OpenAI(base_url=BOSS_URL, api_key="local", max_retries=1, timeout=90)
    reply = local_create(client, BOSS_MODEL, _purpose="local helper (" + system.split(".")[0][:40] + ")", temperature=0.1,
                         max_tokens=max_tokens, extra_body=None if think is True else local_reasoning(think or "off"),
                         messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    return re.sub(r"<think>.*?</think>", "", reply.choices[0].message.content or "", flags=re.S).strip()


WALL_SYSTEM = """You look at a task an assistant on a Windows PC worked on, and say whether a missing tool was its wall.
- "tool": it needed an ability none of its tools give: reading a kind of file or data it can't open, reaching a program,
  device or service it has no way to talk to, a conversion or calculation it can't do. A small Python program could give it.
  This counts even when it got there anyway by improvising a one-off script for that ability (decoding the file itself
  with a PowerShell or Python one-liner): the next time it would have to improvise again.
- "info": it needed facts, a login, access or a choice that only the user has.
- "other": anything else: an app or site misbehaving or blocking it, the user's own limits, it ran out of steps on work
  its tools can do, or it did the task with its tools (in whatever way it chose). A tool it already has is never "tool".
Its tools: {tools}
Reply with JSON only: {{"kind": "tool" or "info" or "other", "name": "a short name for the tool", "does": "what the tool would do, one sentence", "why": "what in the task needed it"}}"""


def wall_check(request: str, answer: str, steps: list[str]) -> dict | None:
    """After a task that didn't get done: was the wall a missing tool? {"name", "does", "why"} if so, else None. Asked of
    the local model (free and private), briefly: it only sorts the wall, IO's workshop and the user do the rest."""
    names = ", ".join(sorted(n for n, a in actions.REGISTRY.items() if "internal" not in a.modes))
    user = (f"Task: {request[:1500]}\n\nWhat it did, last steps:\n" + ("\n".join(steps[-10:]) or "(no tool calls)") +
            f"\n\nIts answer: {clean_summary(answer)[:1500]}")
    try:
        v = loose_json(local_chat(WALL_SYSTEM.format(tools=names), user, max_tokens=700, think="low"))
    except Exception as e:  # the local model busy or down: no proposal this time
        print("wall check:", type(e).__name__, e, file=sys.stderr)
        return None
    if isinstance(v, dict) and v.get("kind") == "tool" and v.get("name") and v.get("does"):
        name = str(v["name"])[:60]
        # a "new" tool named after one IO has (it once proposed about_io for a task that used open_app instead) is noise
        if re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") in actions.REGISTRY:
            return None
        return {"name": name, "does": str(v["does"])[:400], "why": str(v.get("why") or "")[:400]}
    return None


RESOLVE_SYSTEM = """You turn the user's latest chat message into a standalone request, using the conversation before it.
Resolve words like "it", "that", "the moon", "again", "what about..." from what was being discussed (for example after
"What is IO?" answered about the assistant IO, "what about the moon?" means "What is Io, the moon of Jupiter?").
A bare name or short fragment continues the user's last request with that name in it: after "check if I have runescape
installed", "runelite?" means "Check if I have RuneLite installed." (not "What is RuneLite?"); after "what's the weather
in Paris", "and london?" means "What's the weather in London?".
Keep the user's intent and wording otherwise. If the message already stands alone, return it unchanged.
Output only the rewritten request, one line."""


def resolve_followup(task: str, conversation: list[dict]) -> str:
    turns = []
    for m in conversation[-8:]:
        content = m["content"] if isinstance(m["content"], str) else ""
        turns.append(f"{m['role']}: {clean_summary(content)[:400]}")
    # no thinking: with it Qwen spent the 80 tokens reasoning and answered nothing, so follow-ups were never resolved
    text = local_chat(RESOLVE_SYSTEM, "Conversation:\n" + "\n".join(turns) + f"\n\nLatest message: {task}", max_tokens=80, think=False)
    line = text.splitlines()[0].strip().strip('"') if text else ""
    return line if 0 < len(line) <= max(300, 4 * len(task)) else task


def check_work(task: str, steps: list[str], answer: str, constraints: str = "", evidence: str = "", careful: bool = False) -> str:
    """'' when the work looks done; otherwise what is missing, in one sentence. constraints: what the user said not to do;
    evidence: what the director saw on its screenshots (this checker sees no image). careful (High and Max): the
    checker thinks it over briefly instead of answering straight away."""
    verdict = local_chat(CHECK_SYSTEM, f"Request: {task}" + (f"\nThe user's constraints: {constraints}" if constraints else "") +
                         "\n\nActions and results:\n" + "\n".join(steps[-8:]) +
                         # the director's own words, not proof: only the results above show what really happened
                         (f"\n\nThe deciding model's own (unverified) claim: {evidence[:300]}" if evidence else "") +
                         f"\n\nAnswer it wants to give:\n{answer[:1500]}", max_tokens=1500 if careful else 200, think="low" if careful else False)
    if verdict.upper().startswith("NO"):
        return verdict[2:].lstrip(" :.-") or "the request doesn't look done yet"
    return ""


def summarize_steps(task: str, old: list[dict], chat=None) -> str:
    lines = []
    for m in old:
        if m["role"] == "assistant":
            calls = ", ".join(f"{tc['function']['name']}({tc['function']['arguments'][:200]})" for tc in m.get("tool_calls") or [])
            lines.append(f"assistant: {str(m.get('content') or '')[:300]} {calls}".strip())
        elif m["role"] == "tool":
            lines.append(f"result: {str(m['content'])[:600]}")
        else:
            lines.append(f"note: {str(m['content'])[:400]}")
    if chat is not None:  # the brain: a fuller summary of more of the run
        return chat(BRAIN_SUMMARY_SYSTEM, f"Task: {task}\n\nEarlier steps:\n" + "\n".join(lines)[-60000:], 1200)
    return local_chat(SUMMARY_SYSTEM, f"Task: {task}\n\nEarlier steps:\n" + "\n".join(lines), max_tokens=500, think=False)


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
        try:
            return await _run(task, max_steps, options, ask, conversation, images)
        except BaseException as err:
            # Medium: the local model got stuck; the NVIDIA brain takes over from the state the PC is in now, told what
            # was tried, so it carries on instead of starting over. The Escalate comes out wrapped in the MCP clients'
            # task groups (it is raised inside their sessions), so it is unwrapped like app.error_text does
            e = escalation_in(err)
            if e is None:
                raise
            log("escalate", text=str(e)[:300], to="high")
            hint_focus("")
            if callable((options or {}).get("on_escalate")):
                options["on_escalate"]("high")
            return await _run(task + f"\n\n(A first try on the local model stopped: {e}\nCarry on from where things are now: files "
                              "it saved and apps it opened are still there, but IO's browser tab and any dialogs it had open were "
                              "closed, so reopen those if you need them. Don't redo work that is already done.)",
                              max_steps, {**(options or {}), "effort": "high", "escalated": True}, ask, conversation, images)
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
    level = options.get("effort") if options.get("effort") in EFFORT else ("high" if options.get("ask_gemini") else "low")
    eff = EFFORT[level]
    EFFORT_NOW.set(level)  # Glimmer's reasoning for this task (local_create reads it, also in worker threads)
    prune_artifacts()  # whole tool results saved by earlier tasks (spill) go after a week
    if not loop:
        max_steps = min(max_steps, eff["steps"])
    boss = OpenAI(base_url=BOSS_URL, api_key="local", max_retries=3, timeout=300)
    eyes = Eyes()
    # one brain: with a director AI set up and an NVIDIA key, a frontier model on NVIDIA's API runs the whole task with
    # IO's tools itself (native tool calls), like Claude does; no JSON director handing steps to a small local model.
    # The local models stay as eyes (EvoCUA finds where to click) and as the last fallback.
    # who decides: High and Max, NVIDIA's models every step; Medium, the local model unless the task is a hard one (below,
    # once its kind is known) or it gets stuck (Escalate); Low, the local model alone, nothing leaves the PC
    api_ok = bool(eff["api"] and nim.nim_key())
    remote_brain = api_ok and eff["api"] is True
    brain_chain = [(boss, BOSS_MODEL)]
    brain_models: list[str] = []  # the NVIDIA models the brain goes round, in order (Settings > Brain)
    if api_ok:  # Medium's local brain can still ask them (ask_model) and hand over to them
        nim_client = OpenAI(base_url=nim.NIM_URL, api_key=nim.nim_key(), max_retries=0, timeout=300)  # DeepSeek queues ~3 min
        brain_models[:] = [m for m in (options.get("brain_models") or []) if isinstance(m, str) and m.strip()] or list(nim.BRAIN_MODELS)
        brain_chain = [(nim_client, m) for m in brain_models] + brain_chain

    def use_remote_eyes() -> None:
        """look_at_screen: the model Settings picked for it, then the brain's own vision models in order (only when the
        NVIDIA brain runs the task: a local task's screens stay on the PC)."""
        looker = options.get("vision_model") or ""
        eyes.remote = [(nim_client, m) for m in dict.fromkeys(([looker] if looker else []) + brain_models) if nim.is_vision(m)]

    if remote_brain:
        use_remote_eyes()
    log("effort", level=level, brain="nvidia" if remote_brain else "local", reasoning_local=eff["local"],
        reasoning_api=eff["api_reason"] if api_ok else "", steps=max_steps, escalated=bool(options.get("escalated")))
    brain_at = [0]  # the NVIDIA model that answered last; each step starts there
    # NVIDIA models this PC runs itself (Balanced mode's Muse Glimmer): their turns in the brain's order go to the local
    # copy, so they don't wait in NVIDIA's queue or count against its rate limit. Its turns as look_at_screen's vision
    # model stay on NVIDIA, since the local copy loads without its vision part.
    local_twins = {m for m in brain_models if any(w in m for w, name in LOCAL_TWINS.items() if name in boss_file())}

    def text_only(messages):
        """The conversation without pictures, for a brain that reads text only (DeepSeek): it uses look_at_screen."""
        out = []
        for m in messages:
            c = m.get("content") if isinstance(m, dict) else None
            if isinstance(c, list):
                m = {**m, "content": [p if p.get("type") == "text" else
                                      {"type": "text", "text": "[screenshot not shown to you: call look_at_screen to hear what is on screen]"}
                                      for p in c]}
            out.append(m)
        return out

    def one_image(messages):
        """The conversation with only its newest picture, for a model that takes one per request."""
        out, kept = [], False
        for m in reversed(messages):
            c = m.get("content") if isinstance(m, dict) else None
            if isinstance(c, list):
                parts = []
                for p in reversed(c):
                    if p.get("type") == "image_url":
                        if kept:
                            p = {"type": "text", "text": "[earlier screenshot]"}
                        kept = True
                    parts.append(p)
                m = {**m, "content": list(reversed(parts))}
            out.append(m)
        return list(reversed(out))

    def make_brain(at: list, local_fallback: bool = True, stop: threading.Event | None = None, timeout: float | None = None,
                   role: str = "decide next step"):
        """A brain_create for one agent: `at` holds the index of the NVIDIA model that answered it last (each Ultracode
        helper has its own). `stop` ends a helper's retries once Stop is pressed (its thread outlives the task);
        `timeout` caps each request (helpers don't wait out DeepSeek's long queue)."""

        def prepare(model: str, kw: dict) -> dict:
            """The request as this model needs it."""
            kw2 = {k: v for k, v in kw.items() if k != "extra_body"}  # llama-server options mean nothing to NVIDIA
            # this level's reasoning, in the model's own words; Ultracode's helpers think at Medium's, so five of them at
            # once still finish their parts quickly (slower answers hold NVIDIA's request slots longer)
            level = kw2.pop("_level", None) or ("medium" if role == "helper step" else eff["api_reason"])
            think = nim.reasoning(model, level)
            kw2.update(think)
            if think and kw2.get("max_tokens"):
                # a thinking model's thoughts count against max_tokens: at Max, GLM thought 6,000 characters and was cut
                # off before writing the Ultracode plan it was asked for (finish "length", 1,500 tokens)
                kw2["max_tokens"] = int(kw2["max_tokens"]) + REASON_ROOM.get(level, 0)
            if not nim.is_vision(model):
                kw2["messages"] = text_only(kw2["messages"])
            elif model in nim.ONE_IMAGE:
                kw2["messages"] = one_image(kw2["messages"])
            if model in nim.NEEDS_REQUIRED_TOOLS and kw2.get("tools"):
                kw2["tool_choice"] = "required"  # IO's loop always ends in a tool call (done), so nothing is lost
            if model in nim.NO_REQUIRED_TOOLS and kw2.get("tool_choice") == "required":
                kw2.pop("tool_choice")
            # a queue that hasn't answered in 90 s rarely does soon: the next model gets the turn (DeepSeek, the slow
            # text-only one, gets longer). Thinking takes time of its own, at ~25 tokens a second on the free API: the
            # Ultracode plan at Max timed out on both racers at 90 s once it had room to think
            room = REASON_ROOM.get(level, 0) // 40 if think else 0
            kw2["timeout"] = min(timeout or 999, (240 if model in nim.SLOW_QUEUE else 90) + room)
            return kw2

        def ask(client, model: str, purpose: str, kw: dict):
            """One request to one of the brain's models: NVIDIA's, or the local copy of a model this PC runs, when the
            request fits its context (otherwise NVIDIA's copy takes it)."""
            if model in local_twins:
                own = kw.get("_level")
                kw2 = prepare(model, kw)
                kw2.pop("reasoning_effort", None)  # NVIDIA's switch; the local copy gets this level's own, set here because
                kw2["extra_body"] = local_reasoning(("high" if own == "max" else own) if own else  # race threads start bare
                                                    "medium" if role == "helper step" else eff["local"])
                chars, _pics = nim.size_of(kw2["messages"])
                if chars + len(json.dumps(kw2.get("tools") or [])) < LOCAL_TWIN_CHARS:
                    kw2["messages"] = text_only(kw2["messages"])  # it reads text only; it can call look_at_screen
                    kw2["max_tokens"] = min(int(kw2.get("max_tokens") or LOCAL_TWIN_OUT), LOCAL_TWIN_OUT)
                    return local_create(boss, BOSS_MODEL, _purpose=f"{purpose} [local {nim.label(model)}]", **kw2)
            return nim.create(client, _purpose=purpose, model=model, **prepare(model, kw))

        def usable(r, kw: dict) -> bool:
            m = r.choices[0].message
            words = re.sub(r"<\|[^|]*\|>|<think>.*?</think>", "", m.content or "", flags=re.S)
            return bool(m.tool_calls or re.search(r"[^\W\d_]{3}", words))  # not empty, not token junk ("<|close|>!!!!")

        def acts(r, kw: dict) -> bool:
            """Whether a reply does something (a tool call), or tools weren't offered. A race takes the first reply that
            acts; one that only talks waits RACE_TALK_GRACE for an acting one ("Next, I will create the folder..." once
            won by being first and ended the task; rejecting talk outright instead threw away a good final answer)."""
            m = r.choices[0].message
            if m.tool_calls or not kw.get("tools"):
                return True
            return leaked_call(m.content or "", {t["function"]["name"] for t in kw["tools"]}) is not None  # a call written as text

        def race(kw: dict, purpose: str, width: int):
            """The same step sent to up to `width` models at once (healthy, quick-queue ones, in order); the first usable
            answer wins and the others' connections are closed, which frees their slots. None when fewer than two can
            run or none answers (then the models are tried in turn as usual)."""
            now = time.time()
            entrants = [i for i in nim.brain_order(at[0], brain_models)
                        if brain_models[i] not in nim.SLOW_QUEUE and now - nim._health.get(brain_models[i], {}).get("failed", 0) > 120][:width]
            if len(entrants) < 2:
                return None
            box, lock, finished, clients = {}, threading.Lock(), threading.Event(), []
            left = [len(entrants)]
            t0 = time.time()

            def run(i: int) -> None:
                model = brain_models[i]
                client = OpenAI(base_url=nim.NIM_URL, api_key=nim.nim_key(), max_retries=0, timeout=90)
                with lock:
                    clients.append(client)
                t = time.time()
                try:
                    if finished.is_set():
                        return
                    r = ask(client, model, purpose + " (race)", kw)
                    if usable(r, kw):
                        nim.note(model, time.time() - t, True)
                        with lock:
                            if "r" not in box and acts(r, kw):
                                box.update(r=r, i=i, secs=round(time.time() - t, 1))
                                finished.set()
                            elif "talk" not in box:
                                box["talk"] = (r, i, round(time.time() - t, 1), time.time())
                    elif not finished.is_set():
                        nim.note(model, time.time() - t, False)
                except Exception as e:
                    if not finished.is_set():  # a loser cut off by the winner didn't fail
                        nim.note(model, time.time() - t, False, gone=getattr(e, "status_code", 0) == 404)
                finally:
                    with lock:
                        left[0] -= 1
                        if left[0] == 0:
                            finished.set()

            for i in entrants:
                threading.Thread(target=contextvars.copy_context().run, args=(run, i), daemon=True).start()  # the task's level goes along
            while not finished.wait(0.5):
                if stop is not None and stop.is_set():
                    break
                if "talk" in box and time.time() - box["talk"][3] > RACE_TALK_GRACE:
                    break  # nobody acted in time: the talking reply is the answer (often the task's last word)
            finished.set()  # the ones cut off below lost; they didn't fail
            with lock:
                for c in clients:  # cut off the others: their requests end, their slots free up
                    try:
                        c.close()
                    except Exception:
                        pass
                if "r" not in box and "talk" in box:
                    r, i, secs, _at = box["talk"]
                    box.update(r=r, i=i, secs=secs)
            if "r" not in box:
                log("warning", text=f"race: none of {len(entrants)} models answered in {time.time() - t0:.0f}s; trying them in turn")
                return None
            winner = brain_models[box["i"]]
            log("race", winner=winner, secs=box["secs"], entrants=[brain_models[i] for i in entrants])
            at[0] = box["i"]
            return box["r"]

        def create(**kw):
            """One step of the agent loop. With the NVIDIA brain it goes round the brain's models in order (starting
            from the one that answered last) until one answers, two full rounds, or races several at once (Settings);
            the local model is only the main agent's very last resort. Without it, the local model as before."""
            purpose = kw.pop("purpose", role)
            strong = kw.pop("strong", False)  # after a failed step: the strongest planner decides, no race
            if not remote_brain:
                return local_create(boss, BOSS_MODEL, _purpose=purpose, **kw)
            width = min(int(options.get("race_width") or 0), nim.MAX_PARALLEL)
            if width >= 2 and role == "decide next step" and not strong:  # the main agent's steps; helpers go in turn
                r = race(kw, purpose, width)
                if r is not None:
                    return r
            n, last = len(brain_models), None
            order = nim.brain_order(at[0], brain_models)  # the last one that answered first, unless it has turned slow or just failed
            if strong:
                # the quickest model wins a race, often a small one; a step that just failed deserves the best reasoning on
                # offer (healthy ones first, by planner strength), at the cost of a slower answer
                now = time.time()
                order = sorted(range(n), key=lambda j: (now - nim._health.get(brain_models[j], {}).get("failed", 0) <= 120,
                                                        nim.planner_rank(brain_models[j])))
                purpose += " (strongest, after a failure)"
            for attempt in range(2 * n):
                if stop is not None and stop.is_set():
                    raise RuntimeError("stopped")
                if attempt == n and (stop.wait(5) if stop is not None else time.sleep(5)):
                    raise RuntimeError("stopped")  # every model failed once: a short breather before the second round
                i = order[attempt % n]
                client, model = brain_chain[i]
                t_req = time.time()
                try:
                    r = ask(client, model, purpose, kw)
                    if not usable(r, kw):  # nothing in it, token junk, or only "I will...": a failure, next model
                        raise RuntimeError("empty answer")
                    nim.note(model, time.time() - t_req, True)
                    if at[0] != i:
                        log("warning", text=f"the brain is now {nim.label(model)}")
                    at[0] = i
                    return r
                except Exception as e:  # rate limit, outage, queue timeout, a request it can't take: next one
                    last = e
                    nim.note(model, time.time() - t_req, False)
                    if getattr(e, "status_code", 0) == 404:  # not found: skip it for 10 minutes, not 2 (it can be a blip)
                        nim.note(model, 0, False, gone=True)
                    log("warning", text=f"{nim.label(model)} failed ({type(e).__name__}): {nim.scrub(str(e))[:160]}; trying the next model")
                    level = kw.get("_level") or ("medium" if role == "helper step" else eff["api_reason"])
                    if "Timeout" in type(e).__name__ and level in ("max", "high"):
                        # it was still thinking when its time ran out: the next try thinks a notch less (at Max, GLM and
                        # then Kimi each spent 240 s on replacing one constructor, and the step took 8 minutes)
                        kw["_level"] = {"max": "high", "high": "medium"}[level]
                        log("warning", text=f"thinking at {kw['_level']} for this step after the timeout")
                    if getattr(e, "status_code", 0) == 429 and (stop.wait(3) if stop is not None else time.sleep(3)):
                        raise RuntimeError("stopped")  # too many requests: a moment before the next model
            if not local_fallback:
                raise last
            log("warning", text=f"no NVIDIA model answered ({type(last).__name__}); the local model takes this step")
            return local_create(boss, BOSS_MODEL, _purpose=purpose + " (fallback)", **kw)

        return create

    brain_create = make_brain(brain_at)

    def brain_chat(system: str, user: str, max_tokens: int = 1200, purpose: str = "summary") -> str:
        """One plain question to the brain (no tools): summaries and skill playbooks. Bookkeeping, so it thinks at Medium
        at most: at Max, writing a skill playbook timed out on every NVIDIA model (90 s each, eight tries) while they
        thought, and fell to the local model."""
        r = brain_create(messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                         temperature=0.2, max_tokens=max_tokens, purpose=purpose,
                         _level="low" if eff["api_reason"] == "low" else "medium")
        return re.sub(r"<think>.*?</think>", "", r.choices[0].message.content or "", flags=re.S).strip()

    def helper_roster() -> dict[str, tuple]:
        """The models ask_model can consult: short name -> (client, model id, sees screenshots, one-line card). The card
        is what the brain chooses by: what each is good at and how long it has been taking, measured this session."""
        out = {"local": (boss, BOSS_MODEL, False, "the local model on this PC: free, private, no rate limit, text only")}
        for m in brain_models:
            secs = nim._health.get(m, {}).get("avg") or (nim.tests().get(m) or {}).get("secs")
            sees = nim.is_vision(m)
            out[re.sub(r"[^a-z0-9.]+", "-", nim.label(m).lower()).strip("-")] = (
                nim_client, m, sees, f"{nim.label(m)}: {nim.STRENGTHS.get(m, 'general model')}; {'sees screenshots' if sees else 'text only'}"
                + (f"; ~{secs:.0f}s per answer lately" if secs else ""))
        return out

    def ask_model_tool() -> dict:
        """ask_model's definition with this task's models and their cards in it."""
        roster = helper_roster()
        d = actions.tool_schema("ask_model")
        d["function"]["description"] = ("Ask another AI model one question and get its answer (it doesn't act, it advises). Pick by what "
                                        "it's good at: a quick one for a simple check, a strong planner when you're stuck or deciding "
                                        "strategy. Models:\n" + "\n".join(f"- {k}: {v[3]}" for k, v in roster.items()))
        d["function"]["parameters"]["properties"]["model"]["enum"] = list(roster)
        return d

    def ask_helper(name: str, question: str, look: bool, window: str) -> str:
        roster = helper_roster()
        if name not in roster:
            return f"error: no model named {name!r}; pick one of: {', '.join(roster)}"
        client, model, sees, _card = roster[name]
        content: list = [{"type": "text", "text": question.strip() or "What should I do next?"}]
        shot = brain_view(window, bool(loop and eyes.content)) if look and sees and window else ""
        if shot:
            content.insert(0, {"type": "image_url", "image_url": {"url": shot}})
        messages = [{"role": "system", "content": HELPER_SYSTEM.format(goal=task[:600])}, {"role": "user", "content": content}]
        t0 = time.time()
        try:
            if client is boss:
                r = local_create(boss, BOSS_MODEL, _purpose=f"ask_model ({name})", messages=messages, temperature=0.2, max_tokens=1500,
                                 extra_body=local_reasoning("low"))
            else:
                r = nim.create(client, _purpose=f"ask_model ({name})", model=model, messages=messages, temperature=0.2, max_tokens=3000,
                               timeout=240 if model in nim.SLOW_QUEUE else 90, **nim.reasoning(model, eff["api_reason"]))
                nim.note(model, time.time() - t0, True)
        except Exception as e:
            if client is not boss:
                nim.note(model, time.time() - t0, False)
            return f"error: {name} didn't answer ({type(e).__name__}); ask another model"
        answer = re.sub(r"<think>.*?</think>", "", r.choices[0].message.content or "", flags=re.S).strip()
        seen = " (it saw the window)" if shot else (" (it can't see screenshots)" if look and not sees else "")
        return f"{name} answered in {time.time() - t0:.0f}s{seen}: {answer or '(nothing)'}"

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
                if options.get("browser_mode") == "chrome":
                    async def close_tab():  # IO's tab in your Chrome: the extension closes it on DONE_URL (Edge's window goes with its process)
                        try:
                            opened = ctx.tab_open  # (False once the action layer's cleanup has handed it back)
                        except NameError:  # the task ended before its steps began
                            return
                        if opened:
                            await hand_back_tab(br)
                    stack.push_async_callback(close_tab)  # before the session closes (last-in first-out), however the task ends
                # in your own Chrome, closing is left to you
                allowed = BROWSER_TOOLS - {"browser_close"} if options.get("browser_mode") == "chrome" else BROWSER_TOOLS
                for t in (await br.list_tools()).tools:
                    if t.name in allowed:
                        sessions[t.name] = br
                        mcp_tools.append(t)
            except Exception as e:
                log("warning", text=f"browser tools unavailable: {e}")

        standalone = task
        if conversation:
            try:
                standalone = await asyncio.to_thread(resolve_followup, task, conversation) or task
            except Exception as e:
                log("warning", text=f"couldn't resolve the follow-up: {e}")
        if level == "medium" and api_ok and not remote_brain:
            # the route table's own idea of a hard task: driving apps, open-ended work and standing loops already wanted
            # the stronger brain (decider "director"); chat, facts, files, settings and the like stay on the local model
            early = actions.route_of(standalone, loop, bool(images))
            if early.decider == "director":
                remote_brain = True
                use_remote_eyes()
                log("effort", level=level, brain="nvidia", why=f"a {early.kind} task: the NVIDIA models take the hard ones")
        # installed plugins from the Customize page, exposed as "<plugin>_<tool>"
        plugin_tools = await plugins.start_enabled(stack, log=lambda m: log("warning", text=m))
        plugin_defs = {}
        for alias, _, t in plugin_tools:
            d = mcp_to_openai(t, hide=set())  # label/labels are only hidden for Windows-MCP
            d["function"]["name"] = alias
            plugin_defs[alias] = d
        # the local boss has a 32K context: if the enabled plugins' tool lists are too big, keep the ones the task mentions.
        # The NVIDIA brain's 131K holds far more: the 5K cap left Home Assistant's 29 tools out of a task that asked about
        # Home Assistant, and the brain went looking for a configuration.yaml on the disk instead
        plugin_tools = plugins.fit_budget(plugin_tools, plugin_defs, task, log=lambda m: log("warning", text=m),
                                          budget=30000 if remote_brain else 0)
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
        taught = learned.recall(task)  # playbooks IO wrote for itself on earlier runs of this kind of task
        if taught:
            skills = (skills + "\n\n" if skills else "") + taught
            log("skills", text=taught[:600])
        prompt = task
        if notes or skills:
            # next to the task, where a small model actually reads it (it overlooks notes in the system prompt)
            memo = "\n".join(f"- {n['text']}" for n in notes[-MAX_MEMORY:])
            prompt = (f"Your saved notes (use them when relevant):\n{memo}\n\n" if notes else "") + (f"{skills}\n\n" if skills else "") + f"Task: {task}"
        if conversation:  # (resolved above)
            if standalone.strip().lower() != task.strip().lower():
                log("resolved", text=standalone)
                prompt += f"\n\n(This continues the conversation above. In context, the user means: {standalone})"
            else:
                prompt += "\n\n(This continues the conversation above; resolve words like 'it', 'that', or 'again' from it.)"
        # without a date the brain lives at its training time, so nothing tells it that what it remembers of a fast-moving
        # library, a game's patch or a price is old; with one it can reason "that was a year ago, check" (next to the task,
        # not in the system prompt, which stays byte-for-byte the same for the servers' prompt caches)
        prompt += (f"\n\n(Today is {time.strftime('%A')}, {time.strftime('%Y-%m-%d')}. What you know of software, games and prices comes "
                   "from your training, which may be a year or more older: check anything version-specific on this PC or the web.)")
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

        # the action layer (actions.py): one registry generates the local model's menu, SYSTEM's tool ladder, the planner's
        # tool paragraph and the director's nested catalog. data/actions.json {"enabled": false} gives back today's lists.
        layer = actions.enabled()
        ctx = actions.Ctx(options=options, win=win, browser=sessions.get("browser_navigate"), browser_mode=options.get("browser_mode", "edge"),
                          eyes=eyes, ask=None if loop else ask, loop=loop, request=standalone,
                          constraints=actions.constraints_of(standalone, conversation) if layer else [],
                          log=lambda m: log("warning", text=str(m)[:300]))
        found_points = ctx.found_points  # vision answers: loop clicks must come from one (shared with the action library)
        route = actions.route_of(standalone, loop, bool(images))
        vague = layer and route.kind == "vague"
        if vague and isinstance(user_content, str):
            # Qwen otherwise searched folders for 2-3 minutes (and the checker called asking a failure) before asking
            user_content += f"\n\n({VAGUE_NOTE})"
        defs = {t["function"]["name"]: t for t in tools}  # every L1 tool this task can run, by name
        plugin_names = [alias for alias, _, _ in plugin_tools]
        menu_names: list[str] = []  # the local model's menu (registry names), in preference order
        messages: list[dict] = []
        advisor_tool = False  # loops with an advisor (not a director): ask_gemini is on the menu
        browser_where = BROWSER_WHERE.get(options.get("browser_mode", "edge"), BROWSER_WHERE["edge"])

        def executable(n: str) -> bool:
            """Whether this task can run a registry entry: native actions (FILE ones only with file access), and L1 tools
            whose session or branch exists here."""
            a = actions.REGISTRY.get(n)
            if a is None:
                return False
            if a.fn is not None:
                if not options["browser"] and a.group == "WEB":
                    return False
                return options["files"] or (a.group != "FILE" and n != "screenshot")
            if n == "research":
                return bool(options["browser"])  # a hidden researcher (a browser of its own) starts on first use
            if n == "ask_gemini":
                return advisor_tool
            if n in ("ask_model", "notes", "todo", "add_goal"):
                return True  # ask_model: the local model at least, NVIDIA's with a key; notes: IO's own playbooks
            return n in defs

        def task_allows(n: str) -> bool:
            """Whether a call may run in this task, however it was reached (the menu, use(), the director): the toggles,
            and in a loop locked on a window only the loop's own set (never open_app, files or the web)."""
            if n in plugin_defs:
                return True
            if n not in actions.REGISTRY:
                return n in defs or n in ("browser_open", "browser_read")
            if not executable(n):
                return False
            if loop and focus:
                return n in actions.LOOP_MENU or n in LOOP_WINDOW_ACTIONS or n in LOOP_LOCK_EXTRA
            return True

        ctx.allowed = task_allows  # use() and tools() answer by the same rule
        # a long request's text to type goes to the director as TEXT_SLOT (its brief must fit 4,400 characters)
        director_goal, slot_text = text_slot(ctx.request) if layer else (ctx.request, "")

        def apply_menu(names: list) -> None:
            """The local model's tools (compact registry schemas, plus the plugins) and the SYSTEM that matches them."""
            menu_names[:] = [n for n in dict.fromkeys(names) if executable(n)]
            tools[:] = [ask_model_tool() if t["function"]["name"] == "ask_model" else t
                        for t in actions.openai_tools(menu_names)] + [plugin_defs[a] for a in plugin_names]
            if messages:
                messages[0]["content"] = layer_system(menu_names, browser_where, remote_brain)

        if layer:
            first = actions.menu(route, actions.available(ctx, decider="local"), ask=bool(ask) and not loop)
            if loop:
                first = actions.LOOP_MENU + LOOP_WINDOW_ACTIONS + ["done", "tools", "use"]
            elif remote_brain and route.kind not in ("chat", "images", "knowledge"):
                # a frontier brain sees the director's whole action set at once instead of the small model's short menu
                # (the raw clipboard/process/file tools only when the request is about them, as for the director)
                raw = {"Clipboard": r"clipboard", "Process": r"process|task manager|kill|running", "FileSystem": r"\bfile|folder"}
                first = [n for n in actions.available(ctx, decider="director")
                         if n not in raw or re.search(raw[n], standalone, re.I)] + ["ask_user", "done", "tools"]
            if REMEMBER_REQUEST.search(standalone):
                first.append("remember")
            if api_ok and not remote_brain and not loop and "ask_model" not in first:
                first.append("ask_model")  # Medium: a hard question can go to an NVIDIA model without handing over the task
            apply_menu(first)
            system = layer_system(menu_names, browser_where, remote_brain)
        elif "browser_navigate" in sessions:
            system = SYSTEM.replace("{browser_where}", browser_where)
        else:  # browser off or failed to start: point web work at Scrape or the desktop tools instead
            system = re.sub(r"- For anything on a website.*?\n", "- For websites, use Scrape to read a page as text; to interact with one, "
                            "launch the browser with App and use Snapshot, Click and Type.\n", SYSTEM, count=1)
            system = system.replace(" It sees the PC's monitors, not IO's browser tab: answer questions about a web page from browser_snapshot "
                                    "(its title, headings and text).", "")
        messages[:] = [{"role": "system", "content": system}, *conversation, {"role": "user", "content": user_content}]
        head = len(messages)  # everything after this is the task's own working notes, which can be summarized
        steps_log: list[str] = []  # every action with its result, for checking the work before answering
        redos = 0
        log("start", task=task, tools=[t["function"]["name"] for t in tools], tools_chars=len(json.dumps(tools, ensure_ascii=False)),
            route=route.kind if layer else "", constraints=actions.constraints_text(ctx.constraints))
        extra_tools = "\n".join(f"- {alias}: {(t.description or '').split('. ')[0][:120]}" for alias, _, t in plugin_tools)
        last_error, refused_done = "", False
        refused_kinds: dict[str, str] = {}  # kinds of risky action the user said no to in this task -> what was asked
        repeat = {"key": "", "n": 0}  # the same call over and over with nothing in between is a loop
        dead_taps = 0  # taps in a row that changed nothing visible in a loop's window
        last_call: dict = {}  # the previous call and its result: the same again is said out loud, not silently re-run
        screen_points: list = []  # screen points tools have reported (controls, finds): what raw clicks may use
        said_more = False
        route_failures = vision_misses = 0  # errors and unsure results this task (escalation to the director); NOT_FOUND/UNSUPPORTED
        director_saw = ""  # the director's last thoughts: evidence for the work check, which sees no screenshot
        recent: list[str] = []  # recent call keys, to spot a snapshot/read/snapshot/read loop
        last_info = ""  # the last answer-like tool result, used when the model's own summary says nothing
        action_lines: list[str] = []  # short action/result lines, for replanning
        error_streak, replans, last_plan_step, plain_replies = 0, 0, 0, 0

        async def check_before_done(answer: str) -> str:
            """Before reporting back after doing things, check the work; if it falls short, say so and keep going.
            At most MAX_REDOS times per task, and never for plain chat (no tools used)."""
            nonlocal redos
            if steps_log and redos < MAX_REDOS and (unmade := claimed_unmade(answer, steps_log)):
                # any brain: a race can be won by a small model that says it made a file no step wrote (it reported
                # "generated chart.svg" after only reading the CSV)
                redos += 1
                log("check", text=unmade)
                return f"Not done yet: {unmade} Do it and check it, or say plainly that it wasn't done."
            if not steps_log or redos >= MAX_REDOS or (remote_brain and not eff["check"]):
                return ""
            try:
                if layer:  # the resolved request, the user's constraints and what the director saw on screen
                    rules = "; ".join(x for x in (actions.constraints_text(ctx.constraints),
                                                  "the request doesn't say which one, so asking the user and opening nothing until they say is right"
                                                  if vague else "") if x)
                    problem = await asyncio.to_thread(check_work, ctx.request, steps_log, answer, rules, director_saw if director else "", eff["check"])
                else:
                    problem = await asyncio.to_thread(check_work, task, steps_log, answer, "", "", eff["check"])
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
            if layer:  # the tool paragraph comes from the registry; only what it can't say goes here
                if ctx.constraints:
                    parts.append(f"The user said: {actions.constraints_text(ctx.constraints)}.")
                if "browser_navigate" in sessions:
                    where = "IO's own tab in the user's Chrome (tab group 'IO')" if options.get("browser_mode") == "chrome" else "IO's own Edge window"
                    parts.append(f"Web actions work in {where}.")
            elif "browser_navigate" not in sessions:
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
                text = await asyncio.to_thread(planner.plan_local, standalone, context, BOSS_URL, BOSS_MODEL, history,
                                               actions.planner_text(menu_names) if layer else "", local_reasoning("low"))
            except Exception as e:
                log("warning", text=f"planning failed: {e}")
                return
            if text:
                messages.append({"role": "user", "content": (f"A plan for what's left:\n{text}\n\nSkip any step you've already done; follow the "
                                                             "rest, adapting to what you find.") if history else
                                 f"Your plan:\n{text}\n\nFollow it step by step, adapting to what you find."})
            log("plan", source="local", plan=text, secs=round(time.time() - t0, 1), reason="" if text else "conversation, no plan needed")

        researcher = None

        def get_researcher() -> Researcher:
            """The hidden researcher (signed-out Gemini, Google as the fallback), started on first use."""
            nonlocal researcher
            if researcher is None:
                researcher = Researcher(stack)
            return researcher

        async def research_for(question: str) -> str:
            return await get_researcher().ask(question, ctx.request or task)
        ctx.research = research_for  # web_answer falls back to it when Google is blocked or finds nothing

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
                if not layer:
                    tools[:] = [t for t in tools if t["function"]["name"] in LOOP_TOOLS]
                prompt_note = (f"\nThe goal is in the '{focus}' window. find_on_screen and look_at_screen only see its content area, and "
                               "clicks outside it are blocked.")
                if isinstance(messages[head - 1]["content"], str):
                    messages[head - 1]["content"] += prompt_note
            log("loop", goal=task, window=focus)
            if not layer:
                tools.append(RESEARCH_TOOL)
            researcher = Researcher(stack)  # never a web chat AI: Low keeps everything on the PC
            gemini = None if remote_brain else make_director(stack, options)  # one brain: it is the director
            director = bool(gemini) and options.get("advisor_role", "director") == "director"
            if layer:
                # the lean loop set (GAME helpers, vision, a few keys) for a locked window; without one, the general menu
                # plus the vision tools (the GAME helpers need a locked window)
                ctx.focus = focus
                advisor_tool = bool(gemini and not director)
                base = (actions.LOOP_MENU + LOOP_WINDOW_ACTIONS if focus else
                        actions.ROUTES["general"].menu + ["click_on", "hold_on", "look_at_screen", "wait", "research"])
                apply_menu(base + ["done", "tools", "use"] + (["ask_gemini"] if advisor_tool else []))
            if gemini and not director:
                if not layer:
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
                                            actions="\n".join(action_lines[-10:]) or "(none yet)", question=question)
                t0 = time.time()
                try:
                    answer = await gemini.ask(prompt, image)
                except Exception as e:
                    answer = f"error: couldn't reach Gemini: {e}"
                if focus:
                    await asyncio.to_thread(focus_window, focus)  # Chrome may have come to the front: back to the app
                log("gemini", question=question, via=getattr(gemini, "via", "") or type(gemini).__name__, secs=round(time.time() - t0, 1), answer=answer[:800])
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

            # research mode first: learn how the thing works before acting on it (a director gets the notes as long-term
            # guidance: the screen alone doesn't say what the game's progression is)
            try:
                if loop:
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
            # single tasks get the director too (not small talk): it sees the screen and decides the steps until it calls done
            gemini = None
            # every task that acts on the PC goes to the director (one smart model beats a local model's failure loops);
            # only plain chat, questions about attached pictures and general knowledge stay local, where it's instant
            wanted = route.kind not in ("chat", "images", "knowledge") if layer else \
                not images and not (len(task.split()) <= 6 and SMALL_TALK.match(task.strip()))
            if options.get("advisor_role", "director") == "director" and wanted:
                # (a browser session only opens on the first question, so a route that never escalates costs nothing)
                gemini = None if remote_brain else make_director(stack, options)  # one brain: it is the director
            # the user's choice: the director decides every step (it asks the user itself on vague requests); the local
            # models are its eyes and hands (finding and clicking things) and take over only if no director answers
            director = bool(gemini)
            if director and not layer:  # it can look things up too: Gemini signed out (or Google) in a hidden browser
                researcher = Researcher(stack)
                tools.append(RESEARCH_TOOL)

            def window_shot() -> bytes:
                """The window the task works in once one is known, else the whole main screen, as a small JPEG.
                (Not just the front window: "what's on my main monitor" must show the monitor, not one app on it.)"""
                hit = find_window(glow_hint()) if focus_hint else None
                area = hit[1] if hit else displays()[0]
                shot = ImageGrab.grab(bbox=area, all_screens=True).convert("RGB")
                shot.thumbnail((1280, 1280))
                buf = io.BytesIO()
                shot.save(buf, format="JPEG", quality=80)
                return buf.getvalue()
        director_queue: list[tuple[str, dict]] = []
        # a limit hit in an earlier task still counts (a chain only waits when every link is paused)
        director_paused_until = gemini.paused_until() if isinstance(gemini, DirectorChain) else duck_paused_until()
        director_seen = director_rounds = director_guides = 0
        director_tail = False  # the last director batch ended on a whole job or reading that worked (see finish_check)
        director_full, director_plan = False, ""

        def director_names() -> list[str]:
            """What the director may call: every director-mode action this task can run, not just the ones its catalog
            shows, so actions it learned through tools() are accepted. A loop locked on a window keeps its lean set."""
            if loop and focus:
                return [n for n in dict.fromkeys(actions.LOOP_MENU + LOOP_WINDOW_ACTIONS + ["done", "tools"]) if executable(n)]
            names = [n for n in actions.available(ctx, decider="director") if executable(n)]
            # the raw Windows-MCP tools that touch the clipboard, files or processes stay out unless the request is about
            # them: the FILE and PC actions do the same with checks (Clipboard set overwrote the user's clipboard unasked)
            raw_ok = {n for n, word in (("Clipboard", r"clipboard"), ("Process", r"process|task manager|kill|running"),
                                        ("FileSystem", r"\bfile|folder")) if re.search(word, ctx.request, re.I)}
            return [n for n in names if not (loop and actions.REGISTRY[n].group == "GAME" and actions.native(n))  # GAME needs a locked window
                    and (n not in ("Clipboard", "Process", "FileSystem") or n in raw_ok)]

        def director_hint() -> str:
            """Rules the director broke on single tasks: it guessed which document from Recent items (and opened the
            user's own zip), and it took IO's own Duck.ai window for the document."""
            if loop:
                return ""
            return ("Rules: open, close or change only what the request names; if it doesn't say which file or window, "
                    "look for the candidates without opening them, then ask_user which one, listing them. IO's own "
                    "Duck.ai/Chrome window is never the target.\n")

        def director_rules(names: list) -> str:
            """The full standing rules for a director that takes them whole (GLM): the reply contract, the task rules, how
            to batch and the pick-the-right-tool ladder for what it may call."""
            contract = DIRECTOR_STANDING_LAYER if layer else DIRECTOR_STANDING
            ladder = "\n".join(l for l in actions.system_tools_text(names).splitlines() if "use(name" not in l) if layer else ""
            return "\n\n".join(p for p in (contract, director_hint().strip(), DIRECTOR_BATCHING, ladder) if p)

        def director_catalog(names: list, wide: bool = False) -> tuple[str, str]:
            """(top level, expanded groups): in a locked loop the GAME and SEE groups plus the rest of its set on one line;
            otherwise the starred actions per group and the route's groups in full (ACT first: clicking and typing need the
            details most). wide: a roomy director gets every other group expanded after them, since a tools() round costs
            it ~30 s (RAW stays behind tools(): the checked actions come first)."""
            if loop and focus:
                top = actions.catalog_top(names, loop=True)
                rest = [n for n in names if actions.REGISTRY[n].group not in ("GAME", "SEE", "END")]
                if rest:
                    top += "\nALSO " + " ".join(actions.REGISTRY[n].signature(skip=("window", "expect")) for n in rest)
                return top, ""
            groups = sorted(actions.ROUTES["general"].expand if loop else route.expand, key=lambda g: g != "ACT")
            if wide:
                have = {actions.REGISTRY[n].group for n in names}
                groups += [g for g in actions.GROUPS if g not in groups and g in have and g not in ("RAW", "GAME")]
            return actions.catalog_top(names), "\n\n".join(actions.catalog_group(g, names) for g in groups)

        async def direct(step: int) -> str:
            """Director mode: asks the stronger model for the next actions and queues them; returns its thoughts ('' on failure)."""
            nonlocal director_seen, director_rounds, director_full, director_plan, director_guides, director_saw, director_paused_until
            front_before = await asyncio.to_thread(actions.fg)
            await asyncio.to_thread(send_to_back, "Duck.ai")  # its own tab is never what the screenshot should show
            image = await asyncio.to_thread(window_shot) if gemini.takes_images else b""
            if layer:
                allowed = set(director_names())
            else:
                # a loop locked on a window already has a small toolset; anything else gets the compact director list
                usable = [t for t in tools if (loop and focus) or t["function"]["name"] in DIRECTOR_TOOLS | LOOP_TOOLS]
                allowed = {t["function"]["name"] for t in usable}

            def brief(link) -> tuple[str, str]:
                """(message, standing rules) for one director backend. A conversational one keeps one ongoing conversation,
                so it remembers what it tried: after the first round only the new results go in; a fresh conversation (with
                the full brief) when the old one failed or has grown long, or (Duck.ai: max_images a conversation) before a
                picture it can't take. A link without a standing-rules limit (GLM) gets the full rules as its system
                message and a roomy brief; Duck.ai gets 500 characters of rules and a 4,400-character brief."""
                keep = getattr(link, "conversational", False)
                full_rules = keep and not getattr(link, "standing_max", 0)
                roomy = link.max_chars >= DIRECTOR_ROOMY_CHARS
                limit = min(link.max_chars, DIRECTOR_ROOMY_CHARS)
                each = 700 if roomy else 320  # characters per earlier result
                rules = (director_rules(sorted(allowed, key=list(actions.REGISTRY).index)) if full_rules
                         else DIRECTOR_STANDING_LAYER if layer else DIRECTOR_STANDING)
                cap = getattr(link, "max_images", 0)
                # NimDirector keeps nim.KEEP_TURNS turns: a fresh conversation before its first message (the brief) is trimmed
                every = min(20, nim.KEEP_TURNS // 2) if isinstance(link, nim.NimDirector) else 20
                if keep and link.in_chat and (not image or not cap or link.images < cap) and director_rounds % every:
                    new = new_results or ["(no actions ran)"]
                    fresh = new_guides  # research done since its last round
                    # a tools() answer goes whole: it is the catalog page the director asked for
                    head = "".join(f"New from guides: {g[:700]}\n" for g in fresh[-2:])
                    tail = (("\nA new screenshot is attached." if image else f"\nIO's eyes now see: {last_info[:500]}") +
                            "\nIf the same thing keeps not working, change approach. Next JSON.")
                    results = "\n".join(re.sub(r"\s+", " ", a)[:950 if a.startswith("tools(") else each] for a in new)
                    room = limit - len(head) - len(tail) - 40  # the newest results are the ones that matter
                    prompt = head + "Results of your last actions:\n" + results[-max(800, min(8000 if roomy else 3000, room)):] + tail
                    return prompt[-limit:], rules
                history = "\n".join(re.sub(r"\s+", " ", a)[:each] for a in steps_log[-24 if roomy else -14:]) or "(none yet: this is the start)"
                if layer:
                    top, expansions = director_catalog(sorted(allowed, key=list(actions.REGISTRY).index), wide=roomy)
                    # with Duck.ai the rules live in its standing instructions; if it stopped answering in JSON, send them inline again
                    inline = not keep or (director_full and not full_rules)
                    return fit_director_prompt(
                        limit, DIRECTOR_PROMPT_LAYER if inline else DIRECTOR_BRIEF_LAYER, budgets=DIRECTOR_BUDGETS_ROOMY if roomy else DIRECTOR_BUDGETS,
                        goal=f"Goal (it never ends; the user stops it): {task}" if loop else f"Task (do it, then call done with the answer for the user): {director_goal}",
                        hint="" if full_rules else director_hint(),  # (in its rules)
                        constraints=f"Constraints (the user's own words, never break them): {actions.constraints_text(ctx.constraints)}\n" if ctx.constraints else "",
                        shot="A screenshot of the window IO works in is attached.\n" if image else "",
                        plan=f"Your plan so far: {director_plan}\n\n" if director_plan else "",
                        guide=("What guides say about it (for long-term planning):\n" + "\n---\n".join(guide[-2:]) + "\n\n") if loop and guide else "",
                        screen="" if image else f"What IO's eyes last saw on screen:\n{last_info or '(nothing yet)'}\n\n",
                        history=history, catalog=top, expansions=expansions), rules
                return fit_director_prompt(
                    limit, DIRECTOR_BRIEF if keep and (full_rules or not director_full) else DIRECTOR_PROMPT,
                    goal=f"Goal (it never ends; the user stops it): {task}" if loop else
                    f"Task (do it, then call done with the answer for the user): {task}", shot="A screenshot of the window IO works in is attached.\n" if image else "",
                    # its own plan and the research carry over into a fresh conversation, which has no memory
                    plan=f"Your plan so far: {director_plan}\n\n" if director_plan else "",
                    guide=("\n---\n".join(guide[-2:]) if loop else "") or "(none)",
                    screen="" if image else f"What IO's eyes last saw on screen:\n{last_info or '(nothing yet)'}\n\n",
                    history=history, catalog=tool_catalog(usable)), rules

            # taken before the counter moves on: brief() runs later, inside the chain call (reading steps_log[director_seen:]
            # there gave every follow-up "(no actions ran)", and the director concluded IO was stuck)
            new_results, new_guides = steps_log[director_seen:], (guide[director_guides:] if loop else [])
            director_seen, director_rounds = len(steps_log), director_rounds + 1
            director_guides = len(guide) if loop else 0
            t0 = time.time()
            try:
                if isinstance(gemini, DirectorChain):  # each link it tries gets the brief built for it
                    reply = await gemini.ask(brief, image, keep=True)
                else:
                    prompt, standing = brief(gemini)
                    reply = await (gemini.ask(prompt, image, keep=True, instructions=standing) if gemini.conversational
                                   else gemini.ask(prompt, image))
                if reply.startswith("error") and not reply.startswith("error: limit"):
                    director_rounds = 0  # start over in a new conversation next time
            except Exception as e:
                reply = f"error: {e}"
            via = getattr(gemini, "via", "") or type(gemini).__name__
            if getattr(gemini, "in_browser", False):
                # asking Duck.ai brings its Chrome tab forward: send it behind everything, then the app back to the front
                await asyncio.to_thread(send_to_back, "Duck.ai")
                front_now = await asyncio.to_thread(actions.fg)
                if front_now and front_now.exe in actions.BROWSERS and (front_before is None or front_now.hwnd != front_before.hwnd):
                    # Duck.ai titles its chats after the task ("Notepad task execution"), so its window is the browser window
                    # that came to the front during the round, not one titled Duck.ai: behind everything, and the front back
                    await asyncio.to_thread(window_behind, front_now.hwnd, front_before.hwnd if front_before else 0)
            if focus or focus_hint:
                await asyncio.to_thread(refocus, focus or focus_hint)
            code = getattr(gemini, "last_code", "")
            thoughts, batch = ("", []) if reply.startswith("error") else parse_director(reply, allowed, code)
            if not reply.startswith("error"):
                director_plan = director_plan_of(reply, code) or director_plan
                director_full = not batch  # an answer that isn't usable JSON: the next conversation gets the full rules inline
                if not batch:
                    director_rounds = 0
            director_saw = thoughts or director_saw
            # an unusable reply is logged whole: a cut-off one hid why it didn't parse
            log("director", step=step, via=via, model=getattr(gemini, "model", ""), secs=round(time.time() - t0, 1), thoughts=thoughts,
                actions=[f"{n}({json.dumps(a, ensure_ascii=False)[:100]})" for n, a in batch], error=reply[:4000] if not batch else "")
            if isinstance(gemini, DirectorChain):
                if reply.startswith("error") and (until := gemini.paused_until()):
                    director_paused_until = until
                    log("progress", step=step, n=0, summary="No director can be asked right now: the local model decides on its own"
                        + (" for the rest of this task." if until == math.inf else f" for {max(1, round((until - time.time()) / 60))} min, then IO asks again."))
            elif reply.startswith("error: limit"):
                director_paused_until = time.time() + DUCK_LIMIT_PAUSE
                duck_pause(director_paused_until)
                log("progress", step=step, n=0, summary="Duck.ai's usage limit was reached: Qwen decides on its own for an hour, then IO asks Duck.ai again.")
            director_queue.extend(batch)
            return (thoughts or "(director)") if batch else ""

        async def finish_check():
            """The director's batch ended on a whole job or a reading that worked (calculator, write_in_app, read_window):
            the local model (1-3 s) may end the task with done, instead of a director round (~30 s with GLM) that only
            says done. Anything but a done call is dropped and the director is asked as usual."""
            done_tool = [t for t in tools if t["function"]["name"] == "done"]
            if not done_tool:
                return None
            t0 = time.time()
            try:
                r = await asyncio.to_thread(
                    brain_create, tools=done_tool, temperature=0, max_tokens=400, extra_body=local_reasoning("off"),
                    messages=compact(messages, snaps_kept=snaps_kept) + [{"role": "user", "content": FINISH_CHECK.format(task=ctx.request or task)}])
                calls = r.choices[0].message.tool_calls or []
            except Exception as e:
                log("warning", text=f"finish check failed: {e}"[:300])
                return None
            ok = bool(calls) and calls[0].function.name == "done"
            log("finish_check", done=ok, secs=round(time.time() - t0, 1))
            return r if ok else None

        # attached images: the boss answers from what it sees instead of planning; small talk needs no plan
        small_talk = len(task.split()) <= 6 and bool(SMALL_TALK.match(task.strip()))
        if layer:
            # no plan where one action answers (chat, facts, files, screen) or where the director decides anyway
            plan_now = route.planner == "as_today" or (route.planner == "if_director_off" and not director)
        else:
            plan_now = not images and not small_talk
        if layer:
            async def put_away() -> None:
                """End of the task (done, Stop or an error): IO's tab and any dialog it left open go; what the user asked
                for stays. On the stack, so it runs before the browser session closes."""
                try:
                    await asyncio.wait_for(actions.call("cleanup", {}, ctx), 6)
                except (Exception, asyncio.CancelledError):
                    # a Stop while a task is being stopped is already propagating (a callback can't swallow it); one
                    # pressed during the cleanup of a finished task leaves it finished, with its answer
                    pass
            stack.push_async_callback(put_away)
        if plan_now and not remote_brain:  # a frontier brain plans as it goes; the local planner would only slow it down
            await get_plan()
        compact_at, keep_recent = (LOOP_COMPACT_AT, LOOP_KEEP_RECENT) if loop else (COMPACT_AT, KEEP_RECENT)
        # what fits: the model's context (32K for each local mode) less the fixed prompt, the tool list and the
        # reply, at ~2.5 characters a token (UI trees and JSON tokenize worse than prose)
        fixed = len(json.dumps(tools)) + sum(len(str(m.get("content") or "")) for m in messages[:head])
        # the NVIDIA brain uses all the context it has: the smallest window among the models it may send a step to (a race
        # sends the same conversation to several), less the reply. Everything it has read stays word for word until the
        # run fills ~3/4 of that; only then are the oldest steps folded into a summary
        ctx_tokens = (min(nim.context_tokens(m) for m in brain_models) - BRAIN_MAX_OUT) if remote_brain else await asyncio.to_thread(model_context)
        room = max(6000, int((ctx_tokens - 1400) * 2.5) - fixed)
        compact_at = min(compact_at, int(room * 0.6))
        if remote_brain:
            compact_at, keep_recent = int(room * 0.75), 30
        snaps_kept = KEEP_FULL_SNAPSHOTS if room > 40000 else 1
        tool_cap = min(room // 6, 60000) if remote_brain else min(MAX_TOOL_TEXT, room // 3)
        ctx.page_chars = min(tool_cap - 500, 40000) if remote_brain else 6000  # read_page: how much of a page comes back
        keep_full = 10 ** 6 if remote_brain else 2  # tool results kept whole (the rest are trimmed) until the summary
        progress_notes = 0
        if not loop:
            focus = ""
        learned_at = [0]  # len(steps_log) when the playbook was last updated

        def learn_now(outcome: str) -> None:
            """The brain rewrites this task's playbook from the run so far, in the background (never slows the task)."""
            if not remote_brain or options.get("learn") is False or len(steps_log) - learned_at[0] < learned.MIN_STEPS:
                return  # learn=False: the benchmark's runs (they had taught IO playbooks about its own sandbox tasks)
            learned_at[0] = len(steps_log)
            steps, window = list(steps_log), (focus if loop else focus_hint) or ""

            def work() -> None:
                try:
                    name = learned.learn(lambda sy, us: brain_chat(sy, us, 2000, "learn a skill"), standalone, steps, outcome, window)
                    if name:
                        print(json.dumps({"event": "learned", "skill": name, "steps": len(steps)}), flush=True)
                except Exception as e:
                    print(f"couldn't learn from the run: {e}", flush=True)

            threading.Thread(target=contextvars.copy_context().run, args=(work,), daemon=True).start()

        async def ultracode() -> None:
            """Ultracode: the brain writes a plan, sub-agents do its parallel parts (ULTRA_MAX_AGENTS at once, each with its
            own hidden browser and its own NVIDIA model to start on), and their findings go into the main agent's request.
            The main agent then does the desktop parts, with every usual check, and answers."""
            t0 = time.time()
            try:
                limits = actions.constraints_text(ctx.constraints)
                # a plan of parts, not the work itself: at Max reasoning GLM thought past 240 s without writing it
                r = await asyncio.to_thread(brain_create, temperature=0.2, max_tokens=1500, purpose="ultracode plan", _level="medium", messages=[
                    {"role": "system", "content": ULTRA_PLAN},
                    {"role": "user", "content": f"Request: {standalone}" + (f"\nThe user's limits: {limits}" if limits else "")}])
                subs = ultra_subtasks(loose_json(re.sub(r"<think>.*?</think>", "", r.choices[0].message.content or "", flags=re.S)))
            except Exception as e:
                log("warning", text=f"Ultracode couldn't plan ({type(e).__name__}); working step by step")
                return
            par = [s for s in subs if s["kind"] == "parallel"]
            desk = [s for s in subs if s["kind"] == "desktop"]
            log("plan", source="ultracode", secs=round(time.time() - t0, 1), subtasks=subs,
                plan="\n".join(f"{i}. {s['goal']}" + (" (main agent)" if s["kind"] == "desktop" else "") for i, s in enumerate(subs, 1)))
            if len(par) < 2:
                log("warning", text="Ultracode: nothing here is worth splitting up; working step by step")
                return
            names = [n for n in ULTRA_SUB_TOOLS if executable(n) and task_allows(n)]
            stools = actions.openai_tools(names) + [ULTRA_DONE]
            sub_steps = max(5, min(15, max_steps // 2))
            google = {"ok": "web_search" in names}  # shared: once Google asks for a check, no helper searches it again
            gate = asyncio.Semaphore(ULTRA_MAX_AGENTS)
            stop = threading.Event()  # set when this phase ends (Stop included): helper threads still running give up
            slots: asyncio.Queue = asyncio.Queue()  # browser profile slots: one per helper working at a time
            for k in range(ULTRA_MAX_AGENTS):
                slots.put_nowait(k)
            results: dict[str, str] = {}
            log("agents", agents=[{"id": s["id"], "label": s["goal"][:90]} for s in par])

            async def sub_agent(n: int, sub: dict) -> None:
                async with gate:
                    slot = slots.get_nowait()  # the gate guarantees one is free
                    AGENT.set({"id": sub["id"], "label": sub["goal"][:90], "stop": stop})  # this task's own context: tags its records
                    # every helper starts on GLM (measured 2026-10-03: it took 4 helpers at once without slowing; Kimi K3
                    # sometimes stops with an empty answer even when a tool call is required, DeepSeek takes minutes)
                    helper = options.get("helper_model") or ""
                    first = [brain_models.index(helper) if helper in brain_models else 0]
                    brain = make_brain(first, local_fallback=False, stop=stop, timeout=180, role="helper step")
                    t1, status, answer, last, nudged = time.time(), "done", "", "", False
                    try:
                        async with asyncio.timeout(ULTRA_HELPER_SECS), AsyncExitStack() as sub_stack:  # entered and left in this task (the MCP client needs that)
                            sctx = dataclasses.replace(
                                ctx, win=None, ask=None, research=None, tab_open=False, opened=set(), dialogs=set(), found_points=[],
                                hud=[], game_cache={}, fails={}, last_key="", allowed=lambda name: name in names,
                                browser=SubBrowser(sub_stack, slot) if {"web_search", "read_page"} & set(names) else None)
                            inputs = "".join(f"\n\nResult of {d} (another helper): {results.get(d, '')[:2500]}" for d in sub["deps"])
                            msgs = [{"role": "system", "content": ULTRA_SUB_SYSTEM},
                                    {"role": "user", "content": f"The whole request (for context; other helpers do the other parts): {standalone}"
                                                                f"\n\nYour subtask: {sub['goal']}{inputs}"}]
                            for step in range(1, sub_steps + 1):
                                # a tool call every turn (done included): a helper can't end on "Let me search for...".
                                # Each turn tries GLM first again: a helper that once fell back to DeepSeek
                                # shouldn't spend a minute a turn there for the rest of its work
                                first[0] = brain_models.index(helper) if helper in brain_models else 0
                                tools_now = stools if google["ok"] else [t for t in stools if t["function"]["name"] != "web_search"]
                                r = await asyncio.to_thread(brain, messages=msgs, tools=tools_now, temperature=0.2, max_tokens=1200)
                                m = r.choices[0].message
                                # a few calls a turn, and only whole ones: a reply cut off mid-call has broken JSON that
                                # NVIDIA then refuses in every later request of this conversation
                                calls = [c for c in (m.tool_calls or []) if strict_json(c.function.arguments or "{}")][:ULTRA_CALLS_PER_TURN]
                                msgs.append({"role": "assistant", "content": m.content or "",
                                             **({"tool_calls": [{"id": c.id, "type": "function", "function": {
                                                 "name": c.function.name, "arguments": c.function.arguments or "{}"}} for c in calls]} if calls else {})})
                                if not calls:
                                    text = re.sub(r"<think>.*?</think>", "", m.content or "", flags=re.S).strip()
                                    if text and not (ANNOUNCING.match(text) and not nudged):
                                        answer = text  # its answer in plain words (not "Let me search for...")
                                        break
                                    nudged = True
                                    msgs.append({"role": "user", "content": "Call a tool now, or done(summary) with what you found."})
                                    continue
                                finished = False
                                for c in calls:
                                    args = loose_json(c.function.arguments or "{}") or {}
                                    if c.function.name == "done":
                                        answer, finished = str(args.get("summary") or "").strip() or last, True
                                        break
                                    t2 = time.time()
                                    if c.function.name not in names:
                                        res = ("error:BLOCKED: helpers can't use that (no mouse, keyboard or windows); "
                                               "say in done what the main agent should do")
                                    elif c.function.name == "web_search" and not google["ok"]:
                                        res = ("error:BLOCKED: Google is asking for a check, so helpers don't search it any more. "
                                               "read_page a source you know (the official site, or https://en.wikipedia.org/wiki/<Topic>)")
                                    else:
                                        res = actions.constraint_block(c.function.name, args, sctx) or await actions.call(c.function.name, args, sctx)
                                        if c.function.name == "web_search" and "Google asks for a check" in str(res):
                                            google["ok"] = False  # never hammer it: every helper stops searching
                                            res = str(res) + ". Don't search again: read_page a source you know instead."
                                    res = str(res)[:6000] or "ok"
                                    if res.startswith("ok"):
                                        last = res[:1500]
                                    log("tool", step=step, name=c.function.name, args=args, secs=round(time.time() - t2, 1), result=res[:300])
                                    msgs.append({"role": "tool", "tool_call_id": c.id, "content": res})
                                if finished:
                                    break
                            else:
                                answer = "(ran out of steps) " + last
                    except asyncio.CancelledError:
                        raise
                    except TimeoutError:  # one slow queue mustn't hold up the whole task: what it found goes on
                        status, answer = "error", (f"(out of time after {ULTRA_HELPER_SECS}s) What it had found: {last}" if last
                                                   else f"error: out of time after {ULTRA_HELPER_SECS}s")
                    except Exception as e:  # one helper failing never stops the others; what it found so far is kept
                        while isinstance(e, BaseExceptionGroup) and e.exceptions:  # the browser's task group wraps it
                            e = e.exceptions[0]
                        why = f"{type(e).__name__}: {nim.scrub(str(e))[:160]}"
                        status, answer = "error", (f"(stopped early: {why}) What it had found: {last}" if last else f"error: {why}")
                    finally:
                        slots.put_nowait(slot)
                    results[sub["id"]] = answer or "(no result)"
                    log("agent_done", status=status, summary=results[sub["id"]][:600], secs=round(time.time() - t1, 1))

            pending, running, n = list(par), {}, 0
            try:
                async with asyncio.TaskGroup() as tg:  # Stop cancels every helper
                    while pending or running:
                        for s in [s for s in pending if all(d in results for d in s["deps"])]:
                            pending.remove(s)
                            running[tg.create_task(sub_agent(n, s))] = s["id"]
                            n += 1
                        if not running:
                            break
                        finished_tasks, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                        for t in finished_tasks:
                            running.pop(t, None)
            finally:
                stop.set()  # helper threads still inside a request stop retrying and stop logging
            findings = "\n\n".join(f"[{s['id']}] {s['goal']}\n{results.get(s['id'], '(no result)')}" for s in par)
            steps_log.extend(f"helper {s['id']} -> {results.get(s['id'], '')[:1500]}" for s in par)
            note = ("\n\nUltracode: helpers already did these parts in parallel. Use their findings and don't redo them:\n\n"
                    + findings[:30000] + "\n\n"
                    + ("Now do these desktop steps yourself, in order: " + "; ".join(f"{s['id']}: {s['goal']}" for s in desk)
                       + ". Then call done with the full answer." if desk else
                       "Now check the findings fit together and call done with the full answer for the user (take a quick extra "
                       "look only if something is clearly missing or a helper failed)."))
            if isinstance(messages[head - 1].get("content"), str):
                messages[head - 1]["content"] += note
            else:
                messages.append({"role": "user", "content": note.strip()})
            log("ultra", agents=len(par), secs=round(time.time() - t0, 1))

        if eff["ultracode"] and layer and not loop and route.kind not in ("chat", "images", "knowledge"):
            if not remote_brain:
                log("warning", text="Ultracode needs the NVIDIA brain (Settings > Brain); working step by step")
            elif images:
                log("warning", text="Ultracode skipped: this message works from pictures" +
                    (" from earlier in the chat" if options.get("images_from_earlier") else "") + "; working step by step")
            else:
                await ultracode()

        for step in (itertools.count(1) if loop else range(1, max_steps + 1)):
            if loop and step - last_research >= LOOP_RESEARCH_EVERY and (last_info or director_plan):
                # research mode again: look up whatever the latest look says it's facing, so it doesn't circle
                last_research = step
                try:
                    t0 = time.time()
                    q = await asyncio.to_thread(local_chat, "An assistant is working on the goal below and has seen the screen described below. Write "
                                                "one Google search query (under 12 words) that would explain how to make progress on what the screen "
                                                "shows now (name the game or app itself). Output only the query.",
                                                f"Goal: {task}\nScreen: {(last_info or director_plan)[:800]}", 40, False)
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
            if loop and len(steps_log) - learned_at[0] >= LEARN_EVERY:
                learn_now(f"still going after {len(steps_log)} actions (a loop); last results: " + " | ".join(steps_log[-3:])[:600])
            if loop:  # replan when stuck, as often as needed, but not on a timer: the goal never ends
                stuck = error_streak >= REPLAN_AFTER_ERRORS and step - last_plan_step > REPLAN_AFTER_STEPS
            else:
                # failing calls are being stuck; a long task isn't. The step timer is for the small local brain, which drifts;
                # it told a frontier brain it was "stuck" 10 steps into good research, and the new plan started it over
                stuck = (error_streak >= REPLAN_AFTER_ERRORS or (not remote_brain and step - last_plan_step > REPLAN_AFTER_STEPS)) \
                    and replans < MAX_REPLANS
            if (stuck and error_streak >= REPLAN_AFTER_ERRORS and not loop and level == "medium" and api_ok and not remote_brain
                    and replans >= MEDIUM_LOCAL_REPLANS):  # a long but healthy run keeps its steps (out of steps hands over too)
                raise Escalate("steps kept failing, even after a new plan. Its last actions:\n" + "\n".join(steps_log[-10:]))
            if stuck:
                replans, last_plan_step, error_streak = replans + 1, step, 0
                if remote_brain:
                    # the frontier brain re-plans better than the small local planner, which kept starting over from the
                    # first web search even when shown the results: it takes stock of what it already has instead
                    messages.append({"role": "user", "content": REFLECT_NOTE})
                    log("plan", source="brain", plan="(asked the brain to take stock)", secs=0, reason="failing steps")
                else:
                    await get_plan("\n".join(s[:700] for s in steps_log[-12:]))
            if sum(len(str(m.get("content") or "")) for m in messages[head:]) > compact_at:
                cut = len(messages) - keep_recent
                while cut > head and messages[cut]["role"] != "assistant":  # never split a call from its result
                    cut -= 1
                if cut - head >= 4:
                    try:
                        summary_text = await asyncio.to_thread(summarize_steps, task, messages[head:cut], brain_chat if remote_brain else None)
                        messages[head:cut] = [{"role": "user", "content": f"Progress so far (older steps summarized to save space):\n{summary_text}"}]
                        log("compact", step=step, text=summary_text)
                    except Exception as e:
                        log("warning", text=f"couldn't summarize older steps: {e}")
            if (layer and not loop and gemini and not director and route.director_after is not None
                    and route_failures >= route.director_after):
                # the local model's route keeps failing: the director (it sees the screen) decides for the rest of the task
                director = True
                log("progress", step=step, n=0, summary=f"handing over to the director after {route_failures} failed or unconfirmed steps")
            t0 = time.time()
            pending = None  # director mode: the next queued action stands in for the local model's choice
            if director:
                finished = await finish_check() if director_tail and not director_queue and not loop else None
                director_tail = False
                if finished is not None:
                    pending = finished  # the local model's done call stands in for a director round
                    thoughts = ""
                elif not director_queue and time.time() < director_paused_until:
                    thoughts = ""  # no director can be asked (usage limits): the local model decides until the pause is over
                else:
                    thoughts = "" if director_queue else await direct(step)
                if director_queue:
                    tool_name, tool_args = director_queue.pop(0)
                    call = SimpleNamespace(id=f"director-{step}", function=SimpleNamespace(name=tool_name, arguments=json.dumps(tool_args)))
                    pending = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=thoughts, tool_calls=[call]))])
            # the brain sees the window itself on its own turn (one request instead of a look_at_screen round trip to a
            # second model); a text-only model gets a note to call look_at_screen instead (text_only)
            view_window = (focus if loop else "") or focus_hint  # a loop's locked window, else where the task works now
            t_view = time.time()
            view = await asyncio.to_thread(brain_view, view_window, loop and eyes.content) if remote_brain and view_window else ""
            if view:
                log("view", window=view_window, secs=round(time.time() - t_view, 2), kb=len(view) * 3 // 4 // 1024)

            def seen(msgs: list[dict]) -> list[dict]:
                return msgs + [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": view}},
                                                             {"type": "text", "text": VIEW_NOTE.format(w=view_window)}]}] if view else msgs

            # model calls run in a thread so the web app's event loop stays responsive
            try:
                if pending is not None:
                    response = pending
                elif loop:
                    try:
                        response = await asyncio.to_thread(
                            brain_create,
                            messages=seen(compact(messages, keep=keep_full, trim=4000, snaps_kept=2, cap=tool_cap) if remote_brain else
                                          compact(messages, keep=1, trim=800, snaps_kept=1, cap=tool_cap)), tools=tools,
                            temperature=0.3, max_tokens=BRAIN_MAX_OUT if remote_brain else eff["out"],
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
                    brain_create,
                    # a frontier brain keeps every result of its recent steps word for word (its context holds them;
                    # compact_at folds the oldest into a summary). Trimming them to their first 1,500 characters
                    # cut what it had read off pages whose menus come first (python.org's release dates), so it
                    # read the same pages again and again
                    messages=seen(compact(messages, keep=keep_full, snaps_kept=snaps_kept,
                                          cap=tool_cap if remote_brain else 0)), tools=tools, temperature=0.2,
                    max_tokens=BRAIN_MAX_OUT if remote_brain else eff["out"], strong=error_streak > 0,
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
                            brain_create,
                            messages=compact(messages, keep=1, trim=400, snaps_kept=1, cap=min(tool_cap, 4000)), tools=tools,
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
            if not msg.tool_calls and (leaked := leaked_call(msg.content or "", {t["function"]["name"] for t in tools})):
                # Llama writes a tool call as text (<|python_tag|>{"name": ..., "parameters": ...}): it was taken as the
                # final answer, so a task ended with a JSON blob instead of the file it meant to write
                msg.tool_calls, msg.content = [leaked], ""
            calls, batch_of = split_steps(msg.tool_calls or [])
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
                announcing = re.match(r"(i will|i'll|let me|next,|now i|first,|i am going to|i'm going to)\b", low) or MORE_TO_DO.search(low)
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
                    learn_now(summary)
                    return summary
                plain_replies += 1
                messages.append({"role": "user", "content": "Use a tool, or call the done tool with your answer if the task is complete."})
                continue
            plain_replies = 0

            batch_stop: dict[str, str] = {}  # a steps() batch's id -> why the rest of it was skipped
            # several file/PC reads in one reply start together; each result is still taken in its turn below, through
            # every check a single call gets (one that ends up skipped or blocked just goes unused: it only read)
            prefetched: dict[str, tuple[str, asyncio.Task]] = {}
            if layer:
                reads = []
                for c in calls:
                    if c.function.name in PARALLEL_SAFE and actions.native(c.function.name) and task_allows(c.function.name):
                        a = loose_json(c.function.arguments or "{}") if (c.function.arguments or "").strip() else {}
                        if isinstance(a, dict):
                            reads.append((c, a))
                if len(reads) >= 2:
                    for c, a in reads:
                        prefetched[c.id] = (c.function.name + json.dumps(a, sort_keys=True, ensure_ascii=False),
                                            asyncio.create_task(actions.call(c.function.name, dict(a), ctx)))
                        prefetched[c.id][1].add_done_callback(lambda f: f.cancelled() or f.exception())  # an unused one is quiet
                    log("parallel", step=step, names=[c.function.name for c, _a in reads])
            for c in calls:
                name = c.function.name
                in_batch = batch_of.get(c.id)  # (batch id, position, note) for an action that came from steps()
                if in_batch and in_batch[0] in batch_stop:
                    result = f"skipped: {batch_stop[in_batch[0]]}"
                    log("tool", step=step, name=name, result=result)
                    messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
                    continue
                try:
                    args = json.loads(c.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                for _ in range(4):
                    if not (layer and name == "use"):
                        break
                    # the local model's door to every action: unwrapped first (nested ones too), so everything below
                    # (constraints, confirmation, toggles) sees the real one
                    inner = args.get("args")
                    if isinstance(inner, str):
                        inner = loose_json(inner) or {}
                    name, args = str(args.get("name") or "").strip(), inner if isinstance(inner, dict) else {}
                if layer and pending is not None and slot_text:
                    args = fill_slot(args, slot_text)  # the director wrote Â«TEXTÂ» where the user's long text goes
                if layer and name == "Click" and not args.get("loc") and (args.get("label") or args.get("text") or args.get("name") or args.get("target")):
                    # Click by a control's name is click(target) (one letter's case apart, small models mix them up)
                    args = {"target": str(args.get("label") or args.get("text") or args.get("name") or args.get("target")),
                            **{k: args[k] for k in ("window", "button") if args.get(k)}}
                    name = "click"
                if not actions.native(name):
                    args.pop("expect", None)  # the director's expect= is for the action library's own checks
                elif "expect" in args:
                    args["expect"] = usable_expect(str(args["expect"]))
                    if not args["expect"]:
                        args.pop("expect")
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
                    # games and dashboards repeat on purpose: only a long run of the exact same call is a rut (a batch's
                    # repeats count once: tapping one ore three times in a steps() call is the plan, not a rut)
                    if not (in_batch and in_batch[1]):
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
                    desktop = all(k.split("{")[0] in DESKTOP_READS for k in recent[-6:])
                    recent.clear()
                    result = ("You keep reading the same window without anything changing. Act on what you read (click, type_into, "
                              "hotkeys), wait_until something changes, or answer with what you have." if desktop else
                              "You keep re-reading the same page without getting closer. Do something different: open a more specific page "
                              "(for example the exact article, like https://en.wikipedia.org/wiki/Io_(moon)), click a link by its ref from "
                              "browser_snapshot, or answer with what you have.")
                    log("tool", step=step, name=name, args=args, result=result)
                    messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
                    continue
                if not loop and not (in_batch and in_batch[1]):
                    repeat["n"] = repeat["n"] + 1 if key == repeat["key"] else 1
                    repeat["key"] = key
                if not loop and name != "done" and repeat["n"] >= (3 if name in LOOP_PRONE else 10):
                    # the hint fits what is being repeated: a web-reading tip after nine identical "open the page in the
                    # browser" calls sent a run back to re-reading python.org for facts it already had
                    web = name in LOOP_PRONE and name not in DESKTOP_READS or name in ("read_page", "web_search", "web_answer")
                    result = (f"You have made this exact call {repeat['n'] - 1} times in a row. " +
                              ("Use what you have, try another way (for a fact on a long web page, read_page with find), or call done."
                               if web else "It already ran each time; doing it again changes nothing. Go on to the next part of "
                               "the task (or check its effect another way), or call done."))
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
                    learn_now(summary)
                    return summary

                # what the user said not to do ("dont open it") is refused before anything else, in one place; so is
                # anything this task can't run (a toggle is off, or a locked loop's window set), however it was reached
                why = actions.constraint_block(name, args, ctx) if layer else ""
                if layer and not why and not task_allows(name) and name in actions.REGISTRY:
                    why = (f"error:BLOCKED: {name} isn't available in this task" +
                           (f" (a loop locked on '{focus}' only acts in that window)" if loop and focus else " (turned off in IO's settings)"))
                if not why and name == "Process" and args.get("mode") == "kill" and args.get("name") and not args.get("pid"):
                    why = await asyncio.to_thread(kill_by_name_problem, str(args["name"]))
                if not why and name == "Shortcut" and re.sub(r"\s+", "", str(args.get("shortcut", "")).lower()) == "alt+space":
                    why = "error:BLOCKED: alt+space (the window menu) grabs the mouse pointer | try: window_state(window, state)"
                if why or not options["confirm_risky"]:
                    reason = ""
                elif name in plugin_meta:
                    reason = plugin_risky(name, plugin_meta[name], args)
                else:
                    reason = (actions.risky(name, args, ctx) if layer else "") or risky_reason(name, args, ctx.request if layer else task)
                if reason and reason in (options.get("preapproved") or []):
                    reason = ""  # you approved exactly this in Approvals; this task exists to carry it out
                if reason and options.get("unattended") and callable(options.get("approve_later")):
                    # nobody is watching (a goal, a schedule, a trigger): the step waits in Approvals instead of stopping
                    # the run; a risky command runs on a copy of its folder and the brain gets what it did there
                    result = await options["approve_later"](name, args, reason)
                    log("tool", step=step, name=name, args=args, result=result[:300])
                    messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
                    action_lines.append(f"{name} -> queued for approval")
                    steps_log.append(f"{name}({json.dumps(args, ensure_ascii=False)[:200]}) -> {result[:600]}")
                    if in_batch:
                        batch_stop[in_batch[0]] = f"step {in_batch[1] + 1} waits for the user's approval"
                    continue
                if reason:
                    kind = risk_kind(reason)
                    if kind in refused_kinds:  # the user already said no to this kind of thing: don't ask again
                        answer = "no"
                        result = (f"The user already refused to {refused_kinds[kind]} in this task; this ({reason}) is the same kind "
                                  "of action. Don't try it any other way: call done and say what was not done and why.")
                    else:
                        answer = (await ask(f"The agent wants to {reason}. Allow it? (yes/no)")) if ask else "no"
                        result = f"The user did not allow this action ({reason}). Do not retry it or do it another way; call done saying it wasn't done."
                    if not answer.strip().lower().startswith("y"):
                        refused_kinds.setdefault(kind, reason)
                        log("tool", step=step, name=name, args=args, result=result)
                        messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
                        action_lines.append(f"{name} -> refused by user")
                        steps_log.append(f"{name}({json.dumps(args, ensure_ascii=False)[:200]}) -> refused by the user: {reason}")
                        if in_batch:
                            batch_stop[in_batch[0]] = f"the user refused step {in_batch[1] + 1}"
                        if director:
                            director_queue.clear()  # its plan assumed this would run
                        continue

                native = layer and actions.native(name)
                if loop and focus and (name in ("find_on_screen", "look_at_screen", "click_on", "hold_on")
                                       or (native and "window" in actions.REGISTRY[name].params)):
                    args = {**args, "window": focus}
                elif native and isinstance(args.get("window"), str) and " - " in args["window"]:
                    # titles change under the model ("Untitled - Notepad" becomes "*eggs - Notepad" once it types): an old
                    # title that matches nothing any more means the same app's window
                    if await asyncio.to_thread(actions.resolve, ctx, args["window"]) is None:
                        app_part = args["window"].rsplit(" - ", 1)[-1].strip()
                        if app_part and await asyncio.to_thread(actions.resolve, ctx, app_part) is not None:
                            args = {**args, "window": app_part}
                if name in ("find_on_screen", "look_at_screen", "click_on", "hold_on") and args.get("window"):
                    hint_focus(str(args["window"]))
                elif name == "App" and args.get("name") and args.get("mode", "launch") in ("launch", "switch"):
                    hint_focus(str(args["name"]))
                elif native:
                    # the glow goes where the action works; open_app and write_in_app set it to the real title themselves,
                    # and an action without a window works in the task's window, so the glow stays
                    if args.get("window") and not str(args["window"]).startswith("hwnd") and str(args["window"]).lower() != "web":
                        hint_focus(str(args["window"]))
                    elif name == "open_settings":
                        hint_focus("Settings")
                elif not loop and not (layer and name in GLOW_NEUTRAL):
                    hint_focus("")  # working somewhere else: the glow follows the foreground window
                point = None
                if loop and focus and name in ("Click", "hold", "Scroll", "Move", "Drag"):
                    loc = args.get("loc") or []
                    try:
                        point = (int(float(loc[0])), int(float(loc[1])))
                    except (TypeError, ValueError, IndexError):
                        point = None
                blocked = why
                if not loop and not blocked and name in ("Click", "Type", "hold", "Drag") and args.get("loc"):
                    # a screen point has to come from a tool that reports screen points: the brain's screenshots are
                    # scaled pictures of one window, and it clicked pixel positions read off one, which landed on IO's
                    # own window instead of the web page in Chrome
                    try:
                        pt = (int(float(args["loc"][0])), int(float(args["loc"][1])))
                    except (TypeError, ValueError, IndexError, KeyError):
                        pt = None
                    if pt and not any(abs(pt[0] - x) <= 40 and abs(pt[1] - y) <= 40 for x, y in screen_points + found_points):
                        blocked = (f"error: nothing was done: ({pt[0]}, {pt[1]}) isn't a screen point any tool reported. The screenshots "
                                   "you see are scaled pictures of one window, so positions in them aren't screen coordinates. Use "
                                   "click(target) or type_into(field) with the control's label, list_controls for controls with their "
                                   "screen points, or find_on_screen(description). For a web page in IO's tab: web_click and web_fill.")
                if point and not blocked:
                    area = await asyncio.to_thread(content_rect, focus)
                    if area and not (area[0] <= point[0] < area[2] and area[1] <= point[1] < area[3]):
                        blocked = (f"error: ({point[0]}, {point[1]}) is outside the {focus} content area {area}; nothing was clicked. "
                                   "Get the point from find_on_screen.")
                    elif name in ("Click", "hold") and not any(abs(point[0] - x) <= 40 and abs(point[1] - y) <= 40 for x, y in found_points):
                        blocked = "error: nothing was clicked. Don't guess coordinates: call find_on_screen for what you want, then use its x, y."
                # a tap in a loop's window is checked against the window before and after: "Clicked" only says the mouse
                # moved, not that the game took the tap (noise = what the window changes by on its own in 0.15 s)
                watch = await asyncio.to_thread(content_rect, focus) if loop and focus and name in TAP_ACTIONS and not blocked else None
                if watch:
                    sig0 = await actions.sig_of(watch)
                    await asyncio.sleep(0.15)
                    before = await actions.sig_of(watch)
                    noise = actions.sig_diff(sig0, before)
                if blocked:
                    result = blocked
                elif name == "steps":
                    got, problem = actions.expand_steps(args.get("steps"))
                    result = f"error: {problem}" if not got else "error: call steps directly, not through use()"
                elif native:
                    early = prefetched.pop(c.id, None)
                    if early and early[0] == name + json.dumps(args, sort_keys=True, ensure_ascii=False):
                        result = await early[1]  # started with the other reads of this reply
                    else:
                        if early:
                            early[1].cancel()
                        result = await actions.call(name, args, ctx)
                elif layer and name == "click_on" and not loop and (args.get("window") or focus_hint):
                    # click_on in a known window is click's vision rung: the cover check, the cross-check against the
                    # window's controls and the did-anything-change check come with it (loops keep their tuned path)
                    result = await actions.call("click", {"target": str(args.get("description") or ""), "window": str(args.get("window") or ""),
                                                          "how": "vision"}, ctx)
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
                        covered = False
                        if name != "find_on_screen" and focus and not await asyncio.to_thread(window_on_top_at, focus, pt):
                            await asyncio.to_thread(focus_window, focus)  # something covers the app there: bring it up first
                            if not await asyncio.to_thread(window_on_top_at, focus, pt):
                                result = f"error: another window covers the {focus} window at that spot; nothing was clicked"
                                covered = True
                        if name == "find_on_screen" or covered:
                            pass
                        elif name == "click_on":
                            result = await asyncio.to_thread(quick_click, *pt) + f" (found at {pt[0]}, {pt[1]})"
                        else:
                            result = await asyncio.to_thread(hold_mouse, pt[0], pt[1], args.get("seconds", 2))
                        if same_spot >= 2:
                            result += (" Note: this is the same spot your last searches found. If acting on it didn't do what you wanted, "
                                       "it isn't the thing you're after: look at the screen and try something else, or research how this part works.")
                elif name == "look_at_screen":
                    question, display = str(args.get("question") or ""), int(args.get("display", 0) or 0)
                    if layer and not args.get("window") and (titles := await asyncio.to_thread(display_titles, display)):
                        # what is really open there, so a small model can't describe a desktop that isn't (the "Linux desktop")
                        question = (question or "Describe what is on the screen.") + f" (Open windows on this display, front first: {titles}.)"
                    result = await asyncio.to_thread(eyes.describe, question, display, str(args.get("window") or ""))
                elif name == "browser_read" and not ctx.tab_open:
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
                        ctx.tab_open = ctx.tab_open or not result.startswith("error")
                        if "google." in url and "/search" in url:
                            result = (await search_results(sessions["browser_navigate"])) or result
                        elif DISAMBIGUATION.search(result):
                            result = (await meanings_hint(sessions["browser_navigate"], result, task)) or result
                    except Exception as e:
                        result = f"error: {e}"
                elif name == "todo":
                    items = args.get("items")
                    if isinstance(items, str):
                        items = loose_json("{\"i\": " + items + "}") or {}
                        items = items.get("i") if isinstance(items, dict) else None
                    if not isinstance(items, list) or not items:
                        result = 'error: todo needs items like [{"text": "Write the tests", "status": "in_progress"}]'
                    else:
                        items = [i if isinstance(i, dict) else {"text": str(i), "status": "todo"} for i in items][:30]
                        log("todo", step=step, items=items)  # the panel shows the latest list as the task's checklist
                        n_done = sum(1 for i in items if str(i.get("status", "")).lower() == "done")
                        result = f"ok: todo list updated ({n_done} of {len(items)} done)"
                elif name == "add_goal":
                    if not callable(options.get("add_goal")):
                        result = "error: goals need the IO app (this run has no goal list)"
                    elif not str(args.get("objective") or "").strip():
                        result = "error: add_goal needs the objective"
                    else:
                        g = options["add_goal"](str(args["objective"]), int(args.get("every_minutes") or 30), str(args.get("title") or ""))
                        result = (f"ok: goal '{g['title']}' is set: IO checks on it every {g['every_minutes']} minutes by itself, in its own "
                                  f"chat 'Goal: {g['title']}' (the first check-in starts now). Tell the user that.")
                elif name == "notes":
                    result = learned.get(str(args.get("name") or "")) or (
                        f"error: no notes named {args.get('name')!r}; the names are: " + ", ".join(s["name"] for s in learned.load()))
                elif name == "ask_model":
                    result = await asyncio.to_thread(ask_helper, str(args.get("model") or ""), str(args.get("question") or ""),
                                                     bool(args.get("look")), focus or focus_hint)
                elif name == "ask_gemini" and loop and gemini:
                    result = await consult(str(args.get("question") or "What should I do next?"))
                elif name == "research" and (loop or director or layer):
                    last_research = step
                    try:
                        result = await get_researcher().ask(str(args.get("question") or task), task)
                        if loop and not result.startswith("error"):
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
                    if gemini and not director and not loop:
                        director = True  # the request is clear now: the stronger model does the rest (summaries, writing)
                        log("warning", text="the user answered: handing the rest of the task to the director")
                elif name == "type_text" or (name == "Type" and not args.get("loc")):
                    # Type without a location is the most common small-model slip: type into the focused control instead
                    args = {"text": args.get("text", ""), "press_enter": args.get("press_enter", False)}
                    if layer:
                        # into the app being worked in (focused by hwnd, and only if it isn't in front already: a focus
                        # change taps a key, and Win11 Notepad's key tips would swallow the text), past its "what's new"
                        # popup; typed key by key, so the user's clipboard stays as it was (long text: pasted, then put
                        # back); never into Claude, Discord, IO's panel or a browser window the request doesn't name, and it
                        # stops if the user moves the focus mid-way
                        where = focus or focus_hint
                        target = (await asyncio.to_thread(actions.resolve, ctx, where)) if where else None
                        try:
                            if target is not None:
                                actions.guard_input(ctx, target)
                                await actions.focus(target)
                                await actions.clear_tips(target)  # a popup that asks something: error:COVERED, nothing typed
                            target = actions.fg()
                            if target is None:
                                raise actions.Fail("NOT_FOCUSED", "no window has the keyboard", "focus_window(window) or type_into(field, text, window)")
                            actions.guard_input(ctx, target)
                            how = await asyncio.to_thread(actions.type_text_safe, str(args.get("text", "")), bool(args.get("press_enter")),
                                                          target.exe, target.hwnd)
                            result = f"ok: typed {len(str(args.get('text', '')))} characters into '{target.title[:50]}' via {how}"
                        except actions.Fail as e:
                            result = e.result
                        except Exception as e:
                            result = f"error:UNSUPPORTED: typing failed: {type(e).__name__}: {e}"[:300]
                    else:
                        if focus or focus_hint:  # into the app being worked in, not whatever you happen to be typing in
                            await asyncio.to_thread(focus_window, focus or focus_hint)
                        # paste via clipboard: reliable for any text and keyboard layout
                        await win.call_tool("Clipboard", {"mode": "set", "text": args.get("text", "")})
                        await win.call_tool("Shortcut", {"shortcut": "ctrl+v"})
                        if args.get("press_enter"):
                            await win.call_tool("Shortcut", {"shortcut": "enter"})
                        result = "typed"
                elif (name.startswith("browser_") and name not in ("browser_open", "browser_navigate") and not ctx.tab_open):
                    # any browser call connects to Chrome and opens IO's tab group: only once the task has opened a page
                    result = ("error: IO has no browser tab open in this task. browser_* tools only work on web pages opened "
                              "with browser_open; they can't touch windows, dialogs or Chrome's own tabs on the PC. For those use "
                              "Snapshot, Click, find_on_screen or close_windows.")
                elif name in sessions:
                    if name == "browser_navigate":
                        ctx.tab_open = True
                    if name not in aliases:
                        args = fix_args(name, args)
                    args = match_schema(args, defs.get(name) or plugin_defs.get(name))
                    if name == "Snapshot":
                        # the boss is text-only; skip the screenshot image
                        args = {**args, "use_vision": False, "use_annotation": False}
                    try:
                        # a plugin that stops answering mustn't hang the task (and the queue behind it)
                        timeout = {"read_timeout_seconds": PLUGIN_CALL_TIMEOUT} if name in aliases else {}
                        call_args = {**args, "command": ps_wrap(args["command"])} if name == "PowerShell" and args.get("command") else args
                        if name == "PowerShell" and LAUNCHES.search(str(args.get("command") or "")):
                            # run here, not in Windows-MCP: the MCP client keeps that server in a job it kills, with
                            # everything started inside, when the task ends (three graded runs' web servers died so)
                            result = await asyncio.to_thread(ps_here, call_args["command"])
                        else:
                            result = text_of(await sessions[name].call_tool(aliases.get(name, name), call_args, **timeout))
                        if name == "PowerShell":
                            result = ps_unwrap(result)
                            if "Command execution timed out" in result:
                                # two runs started their web server here first and lost 30+ s to a bare "timed out"
                                result += (" (PowerShell waits for a command to finish, so one that never ends, like a server or a "
                                           "watcher, times out here and is stopped. Run those with start_app.)")
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
                if watch and not last_error:
                    await asyncio.sleep(0.3)  # the game draws the tap's effect
                    mean, frac = actions.sig_diff(before, await actions.sig_of(watch))
                    dead = frac <= max(0.004, 1.5 * noise[1]) and mean <= max(1.0, 1.5 * noise[0])
                    dead_taps = dead_taps + 1 if dead else 0
                    if frac > max(0.08, 3 * noise[1]) and mean > max(3.0, 3 * noise[0]):
                        result += " The screen changed a lot (a popup or a new menu?): look before the next tap."
                        if in_batch:
                            batch_stop[in_batch[0]] = f"the screen changed a lot after step {in_batch[1] + 1}; look again before acting"
                    elif dead:
                        result += " Nothing visible changed: the tap may not have registered, or that spot does nothing."
                        if dead_taps >= DEAD_TAPS_NUDGE and "ask_model" in menu_names:
                            dead_taps = 0
                            result += (f" That's {DEAD_TAPS_NUDGE} taps in a row with no effect: stop tapping and ask_model a strong "
                                       "planner with look=true what this screen needs, or research it.")
                if in_batch:
                    if last_error and not (in_batch[0].startswith("reply:") and name in READ_ONLY):
                        batch_stop[in_batch[0]] = (f"step {in_batch[1] + 1} failed" if not in_batch[0].startswith("reply:") else
                                                   f"{name} (call {in_batch[1] + 1} of this reply) failed, and these may depend on it")
                    result += in_batch[2]
                if not last_error and name not in ("Click", "Type", "hold", "Drag"):
                    found_now = [tuple(int(v) for v in m.groups() if v) for m in SCREEN_POINT.finditer(result)]
                    screen_points[:] = (screen_points + [p for p in found_now if len(p) == 2])[-600:]
                if not loop and not last_error and last_call.get("key") == key and last_call.get("result") == result:
                    # nothing in "(no output)" says the browser opened, so a run opened the same page nine times
                    result += " (The same call gave the same result last step: it has already done this.)"
                last_call.update(key=key, result=result.split(" (The same call")[0])
                if not last_error:
                    refused_done = False
                error_streak = error_streak + 1 if last_error else 0
                # unsure: it ran but couldn't be confirmed. Not a failure for done's sake, but the director's next actions
                # assumed it worked, and it counts toward handing the route over
                unconfirmed = layer and result.startswith("unsure:")
                if director and (last_error or unconfirmed):
                    director_queue.clear()  # the rest of its plan assumed this worked: ask the director again with the result
                # the director's batch ended on a whole job or a reading that worked: the local model may close the task
                director_tail = (pending is not None and not director_queue and result.startswith("ok:") and native
                                 and actions.REGISTRY[name].group in FINISH_GROUPS)
                if layer and (last_error or unconfirmed):
                    route_failures += 1
                if layer and re.match(r"error:(NOT_FOUND|UNSUPPORTED)", result) and not director and not loop:
                    vision_misses += 1
                    menu, added = actions.escalate(menu_names, vision_misses, [n for n in actions.available(ctx) if executable(n)])
                    if added:  # the exact ways keep missing: the vision tools join the local model's menu
                        apply_menu(menu)
                        result += f" (added tools: {', '.join(added)})"
                action_lines.append(f"{name}({json.dumps(args, ensure_ascii=False)[:120]}) -> {result[:120]}")  # for replanning
                steps_log.append(f"{name}({json.dumps(args, ensure_ascii=False)[:200]}) -> {result[:1500]}")
                log("tool", step=step, name=name, args=args, secs=round(time.time() - t1, 1), result=result[:300])
                messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
                if name in ("look_at_screen", "PowerShell", "browser_read") and not last_error:
                    last_info = result
                elif native and result.startswith("ok:") and (actions.REGISTRY[name].group in INFO_GROUPS or name == "game_state"):
                    last_info = result
                if native and name == "close_window" and result.startswith("ok:") and focus_hint and not loop:
                    w_arg = str(args.get("window") or "").lower()
                    if not w_arg or w_arg in focus_hint.lower() or focus_hint.lower() in w_arg:
                        hint_focus("")  # its window is gone: the glow follows the foreground window again

        if level == "medium" and api_ok and not remote_brain and not loop:
            raise Escalate(f"it used all {max_steps} steps without finishing. Its last actions:\n" + "\n".join(steps_log[-10:]))
        log("gave_up", steps=max_steps)
        learn_now(f"ran out of steps ({max_steps}) without finishing")
        return f"stopped after {max_steps} steps without finishing"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task", help="what to do, in plain language")
    parser.add_argument("--max-steps", type=int, default=30)
    args = parser.parse_args()
    print(asyncio.run(run(args.task, args.max_steps)))


# the action library uses this module's helpers (quick_click, ps_wrap, Eyes, ...) through this binding, imported or run
actions.bind(sys.modules[__name__])

if __name__ == "__main__":
    main()
