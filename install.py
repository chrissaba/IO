"""Installs IO for this Windows user like any other app. No admin rights needed; everything is per user.

- Start menu entry, so Windows search finds IO and it can be pinned to the taskbar or Start
- desktop shortcut, and a Startup shortcut (opens in the tray) when "Start with Windows" is on
- an entry in Settings > Apps > Installed apps, whose Uninstall runs this script with --uninstall

Every shortcut carries IO's AppUserModelID, the same one desktop.py gives its process, so Windows
treats the open window, its taskbar button and a pinned icon as one app instead of "Python".

Registry entries go through WMI (StdRegProv), which writes the user's real registry even when IO was
started from inside another packaged app (e.g. by Claude), whose own registry writes Windows keeps private.

    pythonw install.py               install or repair
    pythonw install.py --uninstall   remove the shortcuts and the Apps entry (your files stay)
"""
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import winreg
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_NAME = "IO"
APP_ID = "IO.LocalAssistant"  # AppUserModelID
VERSION = "1.0"
LAYOUT = 3  # bump when the shortcuts or registry entries change, so existing installs are rewritten once
ICON_PNG = HERE / "static" / "io.png"


def _icon_path() -> Path:
    """icon-<hash of io.png>.ico: a new picture gets a new file name, so Windows can't keep showing a cached old icon."""
    try:
        return HERE / f"icon-{hashlib.sha1(ICON_PNG.read_bytes()).hexdigest()[:8]}.ico"
    except OSError:
        return HERE / "icon.ico"


ICON = _icon_path()
PYTHONW = HERE / ".venv" / "Scripts" / "pythonw.exe"
PROGRAMS = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
START_MENU_LINK = PROGRAMS / f"{APP_NAME}.lnk"
TASKBAR_PIN = Path(os.environ["APPDATA"]) / "Microsoft" / "Internet Explorer" / "Quick Launch" / "User Pinned" / "TaskBar" / f"{APP_NAME}.lnk"
STARTUP_LINK = PROGRAMS / "Startup" / f"{APP_NAME}.lnk"
UNINSTALLED = HERE / "logs" / ".uninstalled"  # the user uninstalled: IO may still run (a pin, IO.cmd) but registers nothing


def _desktop() -> Path:
    """The real Desktop folder, which OneDrive or a policy may have moved away from the profile."""
    try:
        from win32com.shell import shell, shellcon

        return Path(shell.SHGetFolderPath(0, shellcon.CSIDL_DESKTOPDIRECTORY, 0, 0))
    except Exception:
        return Path(os.environ["USERPROFILE"]) / "Desktop"


DESKTOP_LINK = _desktop() / f"{APP_NAME}.lnk"
OLD_LINKS = [PROGRAMS / "Startup" / "Desktop Agent.lnk", _desktop() / "Desktop Agent.lnk"]
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_NAME}"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder"
DESCRIPTION = "IO, your local assistant"


# ---------- the user's real registry ----------

class RealRegistry:
    """HKCU through WMI StdRegProv (it runs outside any app container); falls back to winreg."""

    HKU = 0x80000003

    def __init__(self):
        self._reg = None
        self._sid = ""
        try:
            import pythoncom
            import win32api
            import win32com.client
            import win32security

            try:
                pythoncom.CoInitialize()
            except pythoncom.com_error:
                pass
            tok = win32security.OpenProcessToken(win32api.GetCurrentProcess(), 0x8)  # TOKEN_QUERY
            self._sid = win32security.ConvertSidToStringSid(win32security.GetTokenInformation(tok, win32security.TokenUser)[0])
            self._reg = win32com.client.Dispatch("WbemScripting.SWbemLocator").ConnectServer(".", "root\\default").Get("StdRegProv")
        except Exception as e:
            print("WMI registry unavailable, using winreg:", e)

    def _call(self, method, key, **kw):
        p = self._reg.Methods_(method).InParameters.SpawnInstance_()
        p.hDefKey = self.HKU
        p.sSubKeyName = f"{self._sid}\\{key}"
        for k, v in kw.items():
            setattr(p, k, v)
        return self._reg.ExecMethod_(method, p)

    def set_values(self, key: str, strings: dict, dwords: dict) -> None:
        if self._reg:
            self._call("CreateKey", key)
            for name, value in strings.items():
                self._call("SetStringValue", key, sValueName=name, sValue=value)
            for name, value in dwords.items():
                self._call("SetDWORDValue", key, sValueName=name, uValue=int(value))
            return
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
            for name, value in strings.items():
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
            for name, value in dwords.items():
                winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, int(value))

    def get(self, key: str, name: str, kind: str = "string"):
        """kind: string | dword | binary. None when missing."""
        if self._reg:
            method = {"string": "GetStringValue", "dword": "GetDWORDValue", "binary": "GetBinaryValue"}[kind]
            out = self._call(method, key, sValueName=name)
            if out.ReturnValue != 0:
                return None
            return {"string": lambda: out.sValue, "dword": lambda: out.uValue, "binary": lambda: bytes(out.uValue or [])}[kind]()
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                return winreg.QueryValueEx(k, name)[0]
        except OSError:
            return None

    def delete_value(self, key: str, name: str) -> None:
        if self._reg:
            self._call("DeleteValue", key, sValueName=name)
        try:  # also any private (app-container) copy
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, name)
        except OSError:
            pass

    def delete_key(self, key: str) -> None:
        if self._reg:
            self._call("DeleteKey", key)
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except OSError:
            pass


_local = threading.local()  # COM objects belong to the thread that made them (the tray menu runs on its own)


def registry() -> RealRegistry:
    if not hasattr(_local, "registry"):
        _local.registry = RealRegistry()
    return _local.registry


# ---------- checks ----------

def python_visible_to_windows() -> bool:
    """False when the venv's base Python sits in another packaged app's private storage (as happens when uv is
    run from inside such an app): shortcuts started by Explorer could not find it."""
    base = getattr(sys, "_base_executable", sys.executable)
    real = os.path.normcase(os.path.realpath(base))
    return "\\appdata\\local\\packages\\" not in real


NOT_VISIBLE = ("IO's Python is stored inside another app's private storage, so Windows couldn't start IO from the Start menu. "
               "Install Python somewhere ordinary (for example run 'uv python install' from a normal terminal) and point "
               ".venv\\pyvenv.cfg at it, then open IO again.")


def set_process_app_id() -> None:
    """Call before any window exists: the taskbar then groups IO under its own name and icon."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(APP_ID))
    except Exception as e:
        print("could not set the app id:", e)


def ensure_icon() -> None:
    """The .ico is built from static/io.png; a changed picture gets a new file (old ones are removed)."""
    from PIL import Image

    try:
        if not ICON.exists():
            Image.open(ICON_PNG).convert("RGBA").save(ICON, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        for old in [HERE / "icon.ico", *HERE.glob("icon-*.ico")]:
            if old != ICON:
                old.unlink(missing_ok=True)
    except Exception as e:
        print("could not rebuild icon:", e)


def refresh_icon_cache() -> None:
    """Asks Explorer to reload icons, so shortcuts and the taskbar pin show a new icon right away."""
    try:
        subprocess.run(["ie4uinit.exe", "-show"], timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as e:
        print("could not refresh the icon cache:", e)


# ---------- shortcuts ----------

def make_shortcut(link: Path, extra: str = "") -> None:
    """A shortcut that starts IO with pythonw (no console), with IO's icon and app id. extra: more arguments (--hidden)."""
    import pythoncom
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell

    try:
        pythoncom.CoInitialize()  # menu callbacks run on other threads
    except pythoncom.com_error:
        pass  # this thread already uses COM
    sl = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    sl.SetPath(str(PYTHONW))
    sl.SetArguments(f'"{HERE / "desktop.py"}" {extra}'.strip())
    sl.SetWorkingDirectory(str(HERE))
    sl.SetIconLocation(str(ICON), 0)
    sl.SetDescription(DESCRIPTION)
    store = sl.QueryInterface(propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID, pythoncom.VT_LPWSTR))
    store.Commit()
    link.parent.mkdir(parents=True, exist_ok=True)
    sl.QueryInterface(pythoncom.IID_IPersistFile).Save(str(link), 0)


def startup_enabled() -> bool:
    """On when the Startup shortcut exists and Windows' own Startup-apps switch (Settings, Task Manager) isn't off."""
    if not STARTUP_LINK.exists():
        return False
    data = registry().get(APPROVED_KEY, STARTUP_LINK.name, "binary")
    return not (data and data[0] & 1)  # 02/06 = on; an odd first byte (03, 01, 07) = turned off in Windows


def set_startup(on: bool, clear_override: bool = True) -> None:
    if on:
        make_shortcut(STARTUP_LINK, "--hidden")  # at sign-in IO waits in the tray
    else:
        STARTUP_LINK.unlink(missing_ok=True)
    if clear_override:  # an explicit choice in IO replaces the switch in Windows' Startup apps
        registry().delete_value(APPROVED_KEY, STARTUP_LINK.name)


# ---------- Settings > Apps ----------

def _folder_kb() -> int:
    total = 0
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in ("browser-profile", "node_modules")] if root == str(HERE / "data") else dirs
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total // 1024


def register_app() -> None:
    """The entry in Settings > Apps > Installed apps."""
    uninstall = f'"{PYTHONW}" "{HERE / "install.py"}" --uninstall'
    registry().set_values(UNINSTALL_KEY, {
        "DisplayName": APP_NAME,
        "DisplayIcon": str(ICON),
        "DisplayVersion": VERSION,
        "Publisher": os.environ.get("USERNAME", "IO").capitalize(),
        "Comments": DESCRIPTION,
        "InstallLocation": str(HERE),
        "UninstallString": uninstall,
        "QuietUninstallString": uninstall + " --quiet",
    }, {"NoModify": 1, "NoRepair": 1, "EstimatedSize": _folder_kb(), "IOLayout": LAYOUT})


def installed_layout() -> int:
    return int(registry().get(UNINSTALL_KEY, "IOLayout", "dword") or 0)


def _moved() -> bool:
    """The registered folder no longer has IO in it and this copy can run: this is where IO lives now."""
    old = registry().get(UNINSTALL_KEY, "InstallLocation") or ""
    return bool(old) and os.path.normcase(old) != os.path.normcase(str(HERE)) and not (Path(old) / "desktop.py").exists() and PYTHONW.exists()


# ---------- install / uninstall ----------

def install(desktop: bool = True, startup: bool | None = None) -> None:
    """Install or repair. startup=None keeps the current "Start with Windows" choice, including one made in Windows."""
    if not python_visible_to_windows():
        raise RuntimeError(NOT_VISIBLE)
    UNINSTALLED.unlink(missing_ok=True)
    ensure_icon()
    had_startup = STARTUP_LINK.exists() or OLD_LINKS[0].exists()
    for link in OLD_LINKS:  # from before the rename to IO
        link.unlink(missing_ok=True)
    make_shortcut(START_MENU_LINK)
    if desktop or DESKTOP_LINK.exists():
        make_shortcut(DESKTOP_LINK)
    if TASKBAR_PIN.exists():  # your pin is a copy of the Start menu shortcut: keep it in step (icon, paths)
        make_shortcut(TASKBAR_PIN)
    if startup is None:
        set_startup(had_startup, clear_override=False)
    else:
        set_startup(startup)
    register_app()
    refresh_icon_cache()
    (HERE / "logs").mkdir(exist_ok=True)
    (HERE / "logs" / ".installed").touch()


def ensure() -> None:
    """Called each time IO starts: a first run installs (with Start with Windows on), an older or moved install is
    rewritten once (keeping your desktop and startup choices), and a missing Start menu entry is put back."""
    try:
        ensure_icon()
        if UNINSTALLED.exists():
            return
        if not python_visible_to_windows():
            print(NOT_VISIBLE)
            return
        layout = installed_layout()
        if layout == 0 and not (HERE / "logs" / ".installed").exists():
            install(desktop=True, startup=True)
        elif layout < LAYOUT or _moved() or registry().get(UNINSTALL_KEY, "DisplayIcon") != str(ICON):
            install(desktop=False)  # older layout, moved folder, or a new icon picture
        elif not START_MENU_LINK.exists():
            make_shortcut(START_MENU_LINK)
    except Exception as e:
        print("could not install IO's shortcuts:", type(e).__name__, e)


def _message(text: str, error: bool = False) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, 0x10 if error else 0x40)


def _quit_running_io() -> None:
    """Asks a running IO to quit and waits for it to exit, so its files can be deleted afterwards."""
    try:
        req = urllib.request.Request("http://127.0.0.1:8765/api/quit", data=b"{}")
        pid = json.loads(urllib.request.urlopen(req, timeout=5).read() or b"{}").get("pid")
    except Exception:
        return
    if pid:
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
        if h:
            k.WaitForSingleObject(h, 15000)
            k.CloseHandle(h)
            time.sleep(0.5)  # the venv launcher exits just after its child


def uninstall(quiet: bool = False) -> None:
    _quit_running_io()
    for link in (START_MENU_LINK, DESKTOP_LINK, STARTUP_LINK, *OLD_LINKS):
        link.unlink(missing_ok=True)
    registry().delete_value(APPROVED_KEY, STARTUP_LINK.name)
    registry().delete_key(UNINSTALL_KEY)
    (HERE / "logs").mkdir(exist_ok=True)
    (HERE / "logs" / ".installed").unlink(missing_ok=True)
    UNINSTALLED.touch()
    if not quiet:
        _message(f"IO was removed from the Start menu, desktop and Installed apps. Unpin it from the taskbar if you pinned it.\n\n"
                 f"Your chats, settings and the app itself are still in\n{HERE}\nTo remove everything, close IO's local models (llama-server), then delete that folder.")


if __name__ == "__main__":
    if "--uninstall" in sys.argv:
        uninstall(quiet="--quiet" in sys.argv)
    else:
        try:
            install()
            if sys.stderr is not None:
                print("IO is installed: find it in Windows search or the Start menu, and right-click it to pin it.")
            else:
                _message("IO is installed. Find it in Windows search or the Start menu, and right-click it to pin it to the taskbar.")
        except Exception as e:
            _message(f"Couldn't install IO: {e}", error=True)
            raise
