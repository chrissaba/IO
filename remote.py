"""Remote access: IO from your phone, through Tailscale.

IO only ever listens on 127.0.0.1. Tailscale's HTTPS proxy (`tailscale serve`) hands it requests from your own devices
on your tailnet, with the tailnet name (chrispc.<tailnet>.ts.net) as the Host. The desktop window keeps using
127.0.0.1 and works as before. A request for any other host is remote, and needs the token of a paired device: you pair
a phone by typing a six-digit code that IO's Settings shows for ten minutes; the phone keeps a long-lived token in a
secure cookie, and Settings can revoke it. Remote access is off until you turn it on."""
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

HERE = Path(__file__).parent
FILE = HERE / "data" / "remote.json"
COOKIE = "io_device"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
# what a phone may load before it has paired: the pairing page and the app shell (no data in any of them)
PUBLIC_PATHS = ("/pair", "/api/pair", "/manifest.webmanifest", "/sw.js", "/offline.html", "/static/", "/api/ping")
PAIR_SECS = 600
PAIR_TRIES = 5
_lock = threading.Lock()
_pair: dict = {}  # the current code: {"code", "expires", "tries"} (memory only: a restart cancels it)
last_remote = [0.0]  # when a paired device last asked for something (keep-awake holds the PC up after it)


def _load() -> dict:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"devices": []}
    except (OSError, ValueError):
        return {"devices": []}


def _save(data: dict) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(FILE)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def is_local(host: str) -> bool:
    return host.rsplit(":", 1)[0].lower() in LOCAL_HOSTS or host.lower().startswith("[::1]")


# what a proxy adds (Tailscale's among them): a request carrying any of these came from another device, even if a proxy
# rewrote its Host to 127.0.0.1. The desktop window never sends them.
PROXY_HEADERS = (b"x-forwarded-for", b"x-forwarded-host", b"forwarded", b"tailscale-user-login")


def is_remote(scope: dict) -> bool:
    headers = dict(scope.get("headers") or [])
    return not is_local(headers.get(b"host", b"").decode("latin-1")) or any(h in headers for h in PROXY_HEADERS)


def new_code() -> dict:
    """A fresh pairing code (any earlier one stops working)."""
    with _lock:
        _pair.clear()
        _pair.update(code=f"{secrets.randbelow(10 ** 6):06d}", expires=time.time() + PAIR_SECS, tries=0)
        return dict(_pair)


def pair(code: str, name: str) -> str:
    """The new device's token for a right code (each code works once), else ''."""
    with _lock:
        if not _pair or time.time() > _pair["expires"]:
            return ""
        _pair["tries"] += 1
        if _pair["tries"] > PAIR_TRIES:  # guessing: the code is burned and a new one has to be made on the PC
            _pair.clear()
            return ""
        if not secrets.compare_digest(str(code).strip(), _pair["code"]):
            return ""
        _pair.clear()
        token = secrets.token_urlsafe(32)
        data = _load()
        data.setdefault("devices", []).append({"id": secrets.token_hex(4), "name": (name or "Phone").strip()[:40], "hash": _hash(token),
                                               "created": time.time(), "last_seen": time.time()})
        _save(data)
        return token


def new_token(name: str) -> str:
    """A token for a program (another AI, a script) instead of a phone: made on the PC, shown once, revocable like a
    paired phone. It's sent as Authorization: Bearer <token>."""
    token = "io_" + secrets.token_urlsafe(32)
    with _lock:
        data = _load()
        data.setdefault("devices", []).append({"id": secrets.token_hex(4), "name": (name or "API").strip()[:40], "hash": _hash(token),
                                               "kind": "api", "created": time.time(), "last_seen": 0})
        _save(data)
    return token


def device_for(token: str) -> dict | None:
    if not token:
        return None
    h = _hash(token)
    with _lock:
        data = _load()
        for d in data.get("devices", []):
            if secrets.compare_digest(d["hash"], h):
                if time.time() - d.get("last_seen", 0) > 300:  # don't rewrite the file on every poll
                    d["last_seen"] = time.time()
                    _save(data)
                last_remote[0] = time.time()
                return d
    return None


def devices() -> list[dict]:
    return [{**{k: v for k, v in d.items() if k not in ("hash", "push")}, "notifications": bool(d.get("push"))}
            for d in _load().get("devices", [])]


def set_push(device_id: str, subscription: dict | None) -> bool:
    """A device's Web Push subscription (None: it turned notifications off, or its subscription is gone)."""
    with _lock:
        data = _load()
        for d in data.get("devices", []):
            if d["id"] == device_id:
                if subscription:
                    d["push"] = subscription
                else:
                    d.pop("push", None)
                _save(data)
                return True
    return False


def push_targets() -> list[tuple[str, dict]]:
    return [(d["id"], d["push"]) for d in _load().get("devices", []) if d.get("push")]


def revoke(device_id: str) -> bool:
    with _lock:
        data = _load()
        before = len(data.get("devices", []))
        data["devices"] = [d for d in data.get("devices", []) if d["id"] != device_id]
        _save(data)
        return len(data["devices"]) < before


# ---------- Tailscale: is it here, this PC's tailnet address, and its HTTPS proxy to IO ----------

def _tailscale(*args: str, timeout: float = 15) -> tuple[int, str]:
    exe = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe"
    exe = str(exe) if exe.exists() else shutil.which("tailscale")
    if not exe:
        return -1, "Tailscale isn't installed"
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired as e:  # `serve` waits while it asks you to turn HTTPS on: what it said so far is the point
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return -2, out.strip()


def tailscale_status(port: int) -> dict:
    """What Settings > Remote shows: missing / signed_out / ready, the address a phone opens, and whether it serves IO."""
    code, out = _tailscale("status", "--json")
    if code == -1:
        return {"tailscale": "missing"}
    try:
        st = json.loads(out)
    except ValueError:
        return {"tailscale": "error", "note": out[:300]}
    if st.get("BackendState") != "Running":
        return {"tailscale": "signed_out", "note": st.get("BackendState", "")}
    name = str((st.get("Self") or {}).get("DNSName", "")).rstrip(".")
    _, served = _tailscale("serve", "status", "--json")
    return {"tailscale": "ready", "address": f"https://{name}" if name else "",
            "serving": f"127.0.0.1:{port}" in served or f"localhost:{port}" in served}


def tailscale_serve(port: int) -> dict:
    """Has Tailscale serve IO over HTTPS on this PC's tailnet name (it stays set up across restarts). The first time, it
    may answer with a link to turn HTTPS on for your tailnet: that link is shown to you, nothing is done on it."""
    code, out = _tailscale("serve", "--bg", f"http://127.0.0.1:{port}", timeout=20)
    links = re.findall(r"https://login\.tailscale\.com/\S+", out)
    return {"ok": code == 0, "output": out[-600:], "link": links[0] if links else "", **tailscale_status(port)}


def token_of(scope: dict) -> str:
    """The device token a request carries: the cookie, or an Authorization: Bearer header (scripts, shortcuts)."""
    headers = dict(scope.get("headers") or [])
    auth = headers.get(b"authorization", b"").decode("latin-1")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    for part in headers.get(b"cookie", b"").decode("latin-1").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            return v
    return ""
