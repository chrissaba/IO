"""IO, the local desktop agent, as a Windows app.

Runs the control panel server in-process and shows it in its own window (Edge WebView2),
with a tray icon. Closing the window only hides it, so queued and 24/7 tasks keep running;
quit from the tray icon. install.py makes it a normal installed app (Start menu, taskbar,
Settings > Apps); "Start with Windows" in the tray menu adds or removes its Startup shortcut.
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).parent
if sys.stderr is None:  # pythonw has no console; keep output in a log file instead
    (HERE / "logs").mkdir(exist_ok=True)
    sys.stdout = sys.stderr = open(HERE / "logs" / "app.log", "a", encoding="utf-8", buffering=1)

import pystray
import uvicorn
import webview
from PIL import Image
from starlette.responses import JSONResponse
from starlette.routing import Route

import app as panel
import install

URL = f"http://127.0.0.1:{panel.PORT}"
ICON = install.ICON  # icon-<hash>.ico, built by install.ensure_icon()
APP_NAME = "IO"
ICON_PNG = HERE / "static" / "io.png"

window: webview.Window | None = None
tray: pystray.Icon | None = None
server: uvicorn.Server | None = None
quitting = False


def icon_image() -> Image.Image:
    for src in (ICON_PNG, ICON):
        try:
            return Image.open(src).convert("RGBA").resize((64, 64), Image.LANCZOS)
        except Exception as e:
            print("could not load the icon", src.name, e)
    return Image.new("RGBA", (64, 64), (38, 38, 80, 255))


shown = {"startup": False}  # what the tray menu last showed; clicking flips that


def starts_with_windows(_item=None) -> bool:
    shown["startup"] = install.startup_enabled()  # also follows the switch in Windows' Startup apps
    return shown["startup"]


def toggle_startup(_icon, _item) -> None:
    install.set_startup(not shown["startup"])


def show(*_args) -> None:
    if window:
        window.show()
        if frame_hwnd and _user32.IsIconic(frame_hwnd):  # restore() would also un-maximize
            window.restore()
        # let the page know it's visible again, so unread replies get their "New" line
        window.evaluate_js("window.panelHidden = false; window.dispatchEvent(new Event('focus'))")


def quit_app(*_args) -> None:
    global quitting
    quitting = True
    if server:
        server.should_exit = True
    if tray:
        tray.stop()
    if window:
        window.destroy()


def on_closing() -> bool:
    if quitting:
        return True
    window.hide()  # keep running in the tray
    # hidden in the tray isn't "looking at it": replies that land now count as unread
    threading.Thread(target=lambda: window.evaluate_js("window.panelHidden = true"), daemon=True).start()
    return False


def post(path: str, body: dict | None = None) -> None:
    """Calls our own API from other threads, which keeps all task state on the server's event loop."""
    req = urllib.request.Request(f"{URL}{path}", data=json.dumps(body or {}).encode())
    req.add_header("Content-Type", "application/json")
    urllib.request.urlopen(req, timeout=5)


def toggle_pause(_icon, _item) -> None:
    post("/api/pause", {"paused": not panel.status["paused"]})


def emergency_stop(*_args) -> None:
    post("/api/stop_all")
    if tray:
        tray.notify("Stopped the current task and paused the queue.", APP_NAME)


def quick_command() -> None:
    show()
    if window:
        window.evaluate_js("window.focusTask && window.focusTask()")


HOTKEYS = {  # id: (modifiers, virtual key, action)
    1: (0x0001 | 0x0002 | 0x4000, 0x51, quick_command),  # Ctrl+Alt+Q (Ctrl+Alt+Space is often taken)
    2: (0x0001 | 0x0002 | 0x4000, 0x23, emergency_stop),  # Ctrl+Alt+End
}


def hotkey_loop() -> None:
    user32 = ctypes.windll.user32
    for hid, (mods, vk, _action) in HOTKEYS.items():
        if not user32.RegisterHotKey(None, hid, mods, vk):
            print(f"hotkey {hid} is taken by another app")
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == 0x0312 and msg.wParam in HOTKEYS:  # WM_HOTKEY
            try:
                HOTKEYS[msg.wParam][2]()
            except Exception as e:
                print("hotkey action failed:", e)


def notify_finished(task: dict) -> None:
    if tray and panel.state["settings"].get("notify"):
        title = {"done": "Task done", "error": "Task failed", "cancelled": "Task stopped"}.get(task["status"], "Task finished")
        tray.notify((task.get("summary") or task["text"])[:200], title)


def notify_question(task: dict) -> None:
    """The agent is blocked on a question: always tell the user, and bring the window up."""
    if tray:
        tray.notify(task.get("question", "")[:200], f"{APP_NAME} needs your answer")
    threading.Thread(target=show, daemon=True).start()  # called on the server's event loop: GUI calls go elsewhere


def set_window_icon() -> None:
    """pywebview's WinForms window shows the Python icon; swap in ours."""
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, APP_NAME)
    if not hwnd:
        return
    for size, which in ((16, 0), (32, 1)):  # ICON_SMALL, ICON_BIG
        hicon = user32.LoadImageW(None, str(ICON), 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
        if hicon:
            user32.SendMessageW(hwnd, 0x0080, which, hicon)  # WM_SETICON


# ---------- seamless window, like the Claude app ----------
# The page draws its own top bar. The window keeps its normal frame styles, so Windows still gives it
# rounded corners, a shadow, snapping and resizing from the sides and bottom; only the caption area
# is handed to the page (WM_NCCALCSIZE). Dragging, double-click and the top edge go through WindowApi,
# which hands them back to Windows' own move/size loops.

_user32 = ctypes.WinDLL("user32")  # own instance, so these argtypes don't touch pywebview's calls
_comctl32 = ctypes.WinDLL("comctl32")
LRESULT = ctypes.c_ssize_t
SUBCLASSPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, ctypes.c_size_t, ctypes.c_size_t)
_comctl32.SetWindowSubclass.argtypes = [wt.HWND, SUBCLASSPROC, ctypes.c_size_t, ctypes.c_size_t]
_comctl32.DefSubclassProc.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_comctl32.DefSubclassProc.restype = LRESULT
_user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_user32.SendMessageW.restype = LRESULT
_user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_user32.IsIconic.argtypes = [wt.HWND]
_user32.IsZoomed.argtypes = [wt.HWND]
_user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
WM_NCCALCSIZE, WM_SYSCOMMAND = 0x0083, 0x0112
WM_IO_GRAB = 0x8000 + 0x51  # WM_APP: start a native move/size loop on the window's own thread
SC_DRAGMOVE, SC_SIZE = 0xF012, 0xF000
SIZE_EDGES = {"left": 1, "right": 2, "top": 3, "topleft": 4, "topright": 5}
frame_hwnd = 0
last_bar_press = 0.0


class NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", wt.RECT * 3), ("lppos", ctypes.c_void_p)]


class WINDOWPOS(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("after", wt.HWND), ("x", ctypes.c_int), ("y", ctypes.c_int), ("cx", ctypes.c_int), ("cy", ctypes.c_int), ("flags", wt.UINT)]


# WinForms still thinks the window has a caption: after a restore from maximized it re-applies the
# old client size plus a caption's height, so the window grew a little every time. Right after a
# restore, size-only changes are ignored.
restore_guard = {"zoomed": False, "until": 0.0}


@SUBCLASSPROC
def _frame_proc(hwnd, msg, wparam, lparam, _id, _ref):
    if msg == 0x0046 and restore_guard["until"] > time.monotonic():  # WM_WINDOWPOSCHANGING
        wp = ctypes.cast(lparam, ctypes.POINTER(WINDOWPOS)).contents
        if wp.flags & 0x2 and not wp.flags & 0x1:  # SWP_NOMOVE without SWP_NOSIZE
            wp.flags |= 0x1
    if msg == 0x0047:  # WM_WINDOWPOSCHANGED
        zoomed = bool(_user32.IsZoomed(hwnd))
        if restore_guard["zoomed"] and not zoomed and not _user32.IsIconic(hwnd):
            restore_guard["until"] = time.monotonic() + 0.4
        restore_guard["zoomed"] = zoomed
    if msg == WM_NCCALCSIZE and wparam:
        rect = ctypes.cast(lparam, ctypes.POINTER(NCCALCSIZE_PARAMS)).contents.rgrc[0]
        top = rect.top
        result = _comctl32.DefSubclassProc(hwnd, msg, wparam, lparam)
        rect.top = top  # no caption: the page starts at the top edge
        if _user32.IsZoomed(hwnd):  # maximized windows hang over the screen edge by their frame
            dpi = _user32.GetDpiForWindow(hwnd)
            rect.top += _user32.GetSystemMetricsForDpi(33, dpi) + _user32.GetSystemMetricsForDpi(92, dpi)
        return result
    if msg == WM_IO_GRAB:
        if _user32.GetAsyncKeyState(0x01) & 0x8000:  # only while the mouse button is still down
            _user32.ReleaseCapture()
            _user32.SendMessageW(hwnd, WM_SYSCOMMAND, wparam, 0)
        return 0
    return _comctl32.DefSubclassProc(hwnd, msg, wparam, lparam)


def _allow_session_end(_sender, e) -> None:
    """Closing to the tray cancels the close; for a Windows shutdown, restart or sign-out it must not.
    Runs after pywebview's own FormClosing handler, so it has the last word."""
    if str(e.CloseReason) == "WindowsShutDown":
        e.Cancel = False


def install_frame() -> None:
    """Runs on the window's thread before it is first shown."""
    global frame_hwnd
    frame_hwnd = window.native.Handle.ToInt64()
    _comctl32.SetWindowSubclass(frame_hwnd, _frame_proc, 1, 0)
    window.native.FormClosing += _allow_session_end
    _user32.SetWindowPos(frame_hwnd, None, 0, 0, 0, 0, 0x0027)  # SWP_FRAMECHANGED | NOMOVE | NOSIZE | NOZORDER


def tell_page_maximized(value: bool) -> None:
    if window:
        window.evaluate_js(f"window.setMaximized && window.setMaximized({'true' if value else 'false'})")


class WindowApi:
    """Called by the page's top bar as window.pywebview.api.*"""

    def minimize(self):
        _user32.ShowWindowAsync(frame_hwnd, 6)  # SW_MINIMIZE

    def toggle_maximize(self):
        _user32.ShowWindowAsync(frame_hwnd, 9 if _user32.IsZoomed(frame_hwnd) else 3)  # SW_RESTORE / SW_MAXIMIZE

    def is_maximized(self):
        return bool(_user32.IsZoomed(frame_hwnd))

    def close(self):
        on_closing()  # hides to the tray, like the close button always has

    def grab(self, what: str):
        """Mouse went down on the top bar ("move") or the top edge/corners: let Windows run the drag.
        A second press on the bar within the double-click time maximizes or restores instead."""
        global last_bar_press
        if what == "move":
            now = time.monotonic()
            if now - last_bar_press < _user32.GetDoubleClickTime() / 1000:
                last_bar_press = 0.0
                self.toggle_maximize()
                return
            last_bar_press = now
        code = SC_DRAGMOVE if what == "move" else SC_SIZE + SIZE_EDGES.get(what, 0)
        if code != SC_SIZE:
            _user32.PostMessageW(frame_hwnd, WM_IO_GRAB, code, 0)


def theme_background() -> str:
    """The page's background colour for the current Windows theme, shown while the page loads."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            light = winreg.QueryValueEx(key, "AppsUseLightTheme")[0]
    except OSError:
        light = 1
    return "#faf9fc" if light else "#23222a"


async def quit_route(_request) -> JSONResponse:
    """Used by the uninstaller to close IO before it removes the shortcuts."""
    threading.Thread(target=quit_app, daemon=True).start()
    return JSONResponse({"ok": True, "pid": os.getpid()})


async def show_route(_request) -> JSONResponse:
    threading.Thread(target=show, daemon=True).start()  # GUI calls can wait on the page, which this loop serves
    return JSONResponse({"ok": True})


def already_running() -> bool:
    """If another copy is running, ask it to show its window instead of starting a second one."""
    user32 = ctypes.windll.user32
    try:
        # Start, search or a pin gave this launch the right to take the foreground: let the running IO use it,
        # or its window is refused focus and only flashes on the taskbar
        user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        # a refused connection to a closed port takes about 2s on this PC, so the timeout must be longer
        urllib.request.urlopen(urllib.request.Request(f"{URL}/api/show", data=b"{}"), timeout=5)
        for _ in range(20):  # and bring it forward from here too, since this process still holds that right
            hwnd = user32.FindWindowW(None, APP_NAME)
            if hwnd and user32.IsWindowVisible(hwnd):
                user32.SetForegroundWindow(hwnd)
                break
            time.sleep(0.05)
        return True
    except urllib.error.HTTPError:  # something else (app.py on its own) owns the port: use it
        webbrowser.open(URL)
        return True
    except urllib.error.URLError as e:
        return not isinstance(e.reason, ConnectionRefusedError)
    except TimeoutError:
        return True


def main() -> None:
    global window, tray, server
    if already_running():
        return
    # its own taskbar identity (before any window exists), so a pinned IO and the open window are one button
    install.set_process_app_id()
    # Start menu entry (Windows search, pinning), desktop and Startup shortcuts, Settings > Apps entry
    install.ensure()

    panel.app.router.routes.append(Route("/api/show", show_route, methods=["POST"]))
    panel.app.router.routes.append(Route("/api/quit", quit_route, methods=["POST"]))
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        sock.bind(("127.0.0.1", panel.PORT))
    except OSError:
        print("port", panel.PORT, "is in use; is IO already running?")
        return
    server = uvicorn.Server(uvicorn.Config(panel.app, host="127.0.0.1", port=panel.PORT, log_level="warning", timeout_graceful_shutdown=2))
    server_thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    server_thread.start()
    for _ in range(100):  # wait for the server so the window doesn't open on an error page
        if server.started:
            break
        time.sleep(0.1)
    if not server.started:
        print("the server did not start")
        return

    tray = pystray.Icon(
        "io-agent",
        icon_image(),
        APP_NAME,
        menu=pystray.Menu(
            pystray.MenuItem("Open", show, default=True),
            pystray.MenuItem("Pause queue", toggle_pause, checked=lambda _item: panel.status["paused"]),
            pystray.MenuItem("Stop everything  (Ctrl+Alt+End)", emergency_stop),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start with Windows", toggle_startup, checked=starts_with_windows),
            pystray.MenuItem("Quit", quit_app),
        ),
    )
    tray.run_detached()
    panel.finished_listeners.append(notify_finished)
    panel.question_listeners.append(notify_question)
    if panel.state["settings"].get("hotkeys", True):
        threading.Thread(target=hotkey_loop, daemon=True).start()

    start_hidden = "--hidden" in sys.argv
    window = webview.create_window(APP_NAME, URL, width=1000, height=760, min_size=(480, 420), hidden=start_hidden,
                                   js_api=WindowApi(), shadow=False, background_color=theme_background(),
                                   text_select=True)  # pywebview's default makes every word in the page unselectable
    window.events.closing += on_closing
    window.events.before_show += install_frame
    window.events.shown += set_window_icon
    window.events.maximized += lambda: tell_page_maximized(True)
    window.events.restored += lambda: tell_page_maximized(bool(_user32.IsZoomed(frame_hwnd)))
    if start_hidden:
        window.events.loaded += lambda: window.evaluate_js("window.panelHidden = true")
    webview.start(private_mode=False, storage_path=str(HERE / "logs" / "webview"))
    # after Quit: let the server finish its shutdown (the final save), then exit even if a model call is still
    # waiting in a worker thread, so no windowless IO stays behind
    if server:
        server.should_exit = True
    server_thread.join(timeout=5)
    os._exit(0)


if __name__ == "__main__":
    main()
