"""An iPhone for IO: the iOS Simulator on Chris's Mac, driven over SSH (Tailscale), with no window on any monitor.

The Mac runs Xcode's Simulator and Meta's idb (in ~/io-tools there, no Homebrew); IO reaches it with its own SSH key.
The screen comes back as a picture (xcrun simctl io ... screenshot) for EvoCUA to look at and find things on, the same
way IO looks at the PC; taps, swipes, typing and the Home button go through idb, in points (pixels / the screen's
scale). data/iphone.json says where the Mac is: {"host", "user", "key", "udid"} (udid: the simulator to use)."""
import base64
import json
import subprocess
from pathlib import Path

HERE = Path(__file__).parent
CONFIG = HERE / "data" / "iphone.json"
DEFAULTS = {"host": "", "user": "", "key": str(Path.home() / ".ssh" / "io_mac"), "udid": ""}
REMOTE_PATH = "export PATH=$HOME/io-tools:$HOME/Library/Python/3.9/bin:$PATH"
# what people call common apps -> their bundle ids (others are looked up by display name in simctl listapps)
APPS = {"settings": "com.apple.Preferences", "safari": "com.apple.mobilesafari", "photos": "com.apple.mobileslideshow",
        "messages": "com.apple.MobileSMS", "calendar": "com.apple.mobilecal", "maps": "com.apple.Maps",
        "health": "com.apple.Health", "wallet": "com.apple.Passbook", "news": "com.apple.news", "files": "com.apple.DocumentsApp",
        "reminders": "com.apple.reminders", "contacts": "com.apple.MobileAddressBook", "app store": "com.apple.AppStore"}
_dims: dict = {}


def config() -> dict:
    try:
        return {**DEFAULTS, **json.loads(CONFIG.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def configured() -> bool:
    c = config()
    return bool(c["host"] and c["user"] and c["udid"] and Path(c["key"]).exists())


def run(command: str, timeout: float = 60) -> str:
    """One shell command on the Mac; its output, or RuntimeError with what went wrong."""
    c = config()
    p = subprocess.run(["ssh", "-i", c["key"], "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
                        f"{c['user']}@{c['host']}", f"{REMOTE_PATH}; {command}"],
                       capture_output=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if p.returncode != 0:
        err = (p.stderr or p.stdout).decode("utf-8", "replace").strip()
        if "Connection refused" in err or "timed out" in err or "No route" in err:
            raise RuntimeError("the Mac didn't answer over SSH (asleep, off Tailscale, or Remote Login off)")
        raise RuntimeError(err[-400:] or f"exit {p.returncode}")
    return p.stdout.decode("utf-8", "replace")


def ensure_booted() -> None:
    """Boots the simulator if it is off (the first boot takes a minute), and connects idb to it."""
    u = config()["udid"]
    state = run(f"xcrun simctl list devices | grep {u}")
    if "(Booted)" not in state:
        run(f"xcrun simctl boot {u} && xcrun simctl bootstatus {u} -b >/dev/null", timeout=300)
    run(f"idb connect {u} >/dev/null 2>&1 || true")


def dims() -> dict:
    """The screen in pixels and points: {"width", "height", "scale"} (taps are in points)."""
    if not _dims:
        out = run(f"idb describe --udid {config()['udid']} --json")
        d = json.loads(out).get("screen_dimensions") or {}
        _dims.update(width=int(d.get("width", 1206)), height=int(d.get("height", 2622)), scale=float(d.get("density", 3.0)))
    return _dims


def screenshot() -> bytes:
    """The simulator's screen as PNG bytes."""
    u = config()["udid"]
    out = run(f"xcrun simctl io {u} screenshot --type=png /tmp/io_iphone.png >/dev/null 2>&1 && base64 -i /tmp/io_iphone.png", timeout=60)
    return base64.b64decode(out)


def tap(fx: float, fy: float) -> tuple[int, int]:
    """Taps at a point given as fractions of the screen; returns it in points."""
    d = dims()
    x, y = round(fx * d["width"] / d["scale"]), round(fy * d["height"] / d["scale"])
    run(f"idb ui tap --udid {config()['udid']} {x} {y}")
    return x, y


def type_text(text: str) -> None:
    run(f"idb ui text --udid {config()['udid']} {shell_quote(text)}")


def swipe(direction: str) -> None:
    """Swipes the screen: up (scroll down the page), down, left, right."""
    d = dims()
    w, h = d["width"] / d["scale"], d["height"] / d["scale"]
    cx, cy = w / 2, h / 2
    moves = {"up": (cx, h * 0.7, cx, h * 0.3), "down": (cx, h * 0.3, cx, h * 0.7),
             "left": (w * 0.8, cy, w * 0.2, cy), "right": (w * 0.2, cy, w * 0.8, cy)}
    x1, y1, x2, y2 = moves[direction]
    run(f"idb ui swipe --udid {config()['udid']} {round(x1)} {round(y1)} {round(x2)} {round(y2)} --duration 0.3")


def home() -> None:
    run(f"idb ui button --udid {config()['udid']} HOME")


def open_target(target: str) -> str:
    """Opens a link (https://, tel:, maps:...) or an app by name or bundle id; what was opened."""
    u = config()["udid"]
    t = target.strip()
    if "://" in t or t.startswith(("tel:", "mailto:", "sms:")):
        run(f"xcrun simctl openurl {u} {shell_quote(t)}")
        return f"opened {t}"
    bundle = APPS.get(t.lower()) or (t if t.count(".") >= 2 and " " not in t else "")
    if not bundle:
        apps = json.loads(run(f"xcrun simctl listapps {u} | plutil -convert json -o - -"))
        for bid, info in apps.items():
            if t.lower() in (str(info.get("CFBundleDisplayName", "")).lower(), str(info.get("CFBundleName", "")).lower()):
                bundle = bid
                break
    if not bundle:
        raise LookupError(f"no app named {t!r} on the simulator")
    run(f"xcrun simctl launch {u} {bundle} >/dev/null")
    return f"opened {t} ({bundle})"


def shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
