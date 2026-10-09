"""IO's workshop: tools IO builds for itself, with the user's OK twice.

1. A wall. A task can't be done for lack of an ability (the brain says so with propose_tool, or the wall check after a
   task that didn't get done finds it). It becomes an approval: "IO can't do X yet; it could build a tool that does Y."
2. Build. On yes, a task writes workshop/<id>/tool.py and test_tool.py (only there: IO's own program files are read-only
   to it), runs workshop_test until the test passes, then calls tool_ready.
3. Turn on. A second approval shows what the tool does and how it was tested. On yes, the tool's files are pinned by
   their hash and it starts like a plugin (its own process, through workshop_host.py) for every task. If its files
   change afterwards, it stops loading until it's tested and approved again.

Both approvals are made on the PC, never from a paired phone.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
WORKSHOP = HERE / "workshop"
STATE = HERE / "data" / "workshop.json"
HOST = HERE / "workshop_host.py"
TEST_TIMEOUT = 120
NO_WINDOW = 0x08000000
_ID = re.compile(r"[a-z0-9_]{1,32}")
SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.I)

BUILD_BRIEF = """How workshop tools work:
- Write only in {dir}. IO's own program files are read-only to you; about_io lists them, and read_file on them shows
  how IO does things, if that helps.
- tool.py: plain Python functions, one per ability, each with type-hinted parameters (str, int, float, bool, list[str])
  and a docstring whose first line says what it does (the brain reads it to choose the tool). Each returns a str: the
  result as the brain should read it. List them in TOOLS = [function, ...]. Use the standard library, or packages
  already installed in IO's Python ({python}); check a package's real API with api_lookup(of="module") first, since
  what you remember of it may be from another version. No input(), no windows, nothing that keeps running: each call
  finishes within a minute, and raises an exception with a clear message when it can't do its job.
- test_tool.py: calls the functions with real inputs from this PC and asserts on what they return. It runs in that
  folder, so `import tool` works.
- workshop_test(tool="{id}") checks those rules and runs the test. Fix and run it again until it passes.
- Then tool_ready(tool="{id}", summary="what it does and how you tested it"). The user decides whether to turn it on;
  from the next task on, its functions are tools named {prefix}_<function>."""


def python() -> str:
    """IO's own Python (the console one: a tool host talks over stdin/stdout)."""
    exe = Path(sys.executable)
    con = exe.with_name("python.exe")
    return str(con if con.exists() else exe)


def load() -> dict:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("tools", {})
    return data


def save(data: dict) -> None:
    STATE.parent.mkdir(exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")[:32] or "tool"


def prefix(tid: str) -> str:
    """What its tools are called: <prefix>_<function> (the same rule as plugins)."""
    return re.sub(r"[^a-zA-Z0-9]", "", tid)[:16]


def folder(tid: str) -> Path:
    if not _ID.fullmatch(str(tid or "")):
        raise ValueError(f"no workshop tool {str(tid)[:40]!r} (ids are lowercase letters, digits and _)")
    return WORKSHOP / tid


def get(tid: str) -> dict | None:
    return load()["tools"].get(tid)


def update(tid: str, **kw) -> dict:
    data = load()
    entry = data["tools"].setdefault(tid, {"id": tid, "created": time.time()})
    entry.update(kw)
    save(data)
    return entry


def files_hash(tid: str) -> str:
    """One hash over every .py file in the tool's folder: what was tested and approved is exactly what runs."""
    d = folder(tid)
    h = hashlib.sha256()
    for p in sorted(d.glob("*.py")) if d.is_dir() else []:
        h.update(p.name.encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()[:16]


def _words(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2}


def propose(name: str, does: str, why: str, task_id: str = "", source: str = "") -> tuple[dict, bool]:
    """(the workshop entry, whether it's new). The same idea twice (by name, or mostly the same words) is one entry:
    a wall that keeps coming back doesn't keep asking, and a tool the user declined isn't asked about again for a week."""
    data = load()
    words = _words(f"{name} {does}")
    for e in data["tools"].values():
        same = slug(name) == e["id"] or len(words & _words(f"{e.get('name', '')} {e.get('does', '')}")) >= max(3, len(words) * 0.6)
        if same and not (e.get("status") == "declined" and time.time() - e.get("decided", 0) > 7 * 86400):
            return e, False
    tid = slug(name)
    n = 2
    while tid in data["tools"]:
        tid = f"{slug(name)[:29]}_{n}"
        n += 1
    entry = {"id": tid, "name": str(name).strip()[:60], "does": str(does).strip()[:400], "why": str(why).strip()[:400],
             "status": "proposed", "created": time.time(), "task_id": task_id, "source": source}
    data["tools"][tid] = entry
    save(data)
    return entry, True


def build_text(entry: dict) -> str:
    """The task that builds a proposed tool."""
    tid = entry["id"]
    return (f"Build a new tool for yourself in IO's workshop: \"{entry['name']}\". It should: {entry['does']}\n"
            f"Why it's needed: {entry.get('why') or '(not given)'}\n\n" +
            BUILD_BRIEF.format(dir=folder(tid), id=tid, python=python(), prefix=prefix(tid)))


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    return env


def check_sync(tid: str) -> dict:
    """The host's own check of tool.py: it loads, and every tool follows the rules."""
    r = subprocess.run([python(), str(HOST), str(folder(tid)), "--check"], capture_output=True, timeout=60, cwd=str(folder(tid)),
                       env=_env(), creationflags=NO_WINDOW)
    try:
        return json.loads(r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except (ValueError, IndexError):
        err = r.stderr.decode("utf-8", "replace").strip()[-1500:]
        return {"ok": False, "problems": [f"the check itself failed: {err or 'no output'}"], "tools": []}


def test_sync(tid: str) -> dict:
    """Checks the rules, then runs test_tool.py; the result is kept with the hash of the files it tested."""
    d = folder(tid)
    if not (d / "tool.py").is_file():
        return {"ok": False, "output": f"no tool.py in {d} yet"}
    h = files_hash(tid)
    chk = check_sync(tid)
    if not chk.get("ok"):
        result = {"ok": False, "output": "tool.py doesn't follow the workshop's rules yet:\n- " + "\n- ".join(chk.get("problems") or ["?"])}
    elif not (d / "test_tool.py").is_file():
        result = {"ok": False, "output": "write test_tool.py: it calls the tools with real inputs and asserts on what they return"}
    else:
        try:
            r = subprocess.run([python(), "test_tool.py"], capture_output=True, timeout=TEST_TIMEOUT, cwd=str(d), env=_env(),
                               creationflags=NO_WINDOW)
            out = (r.stdout + b"\n" + r.stderr).decode("utf-8", "replace").replace("\r\n", "\n").strip()
            result = {"ok": r.returncode == 0, "output": f"test_tool.py exit code {r.returncode}\n{out[-4000:]}"}
        except subprocess.TimeoutExpired:
            result = {"ok": False, "output": f"test_tool.py ran past {TEST_TIMEOUT} s and was stopped"}
    result.update(hash=h, at=time.time(), tools=chk.get("tools") or [])
    if get(tid):
        update(tid, test=result)
    return result


def enabled(log=print) -> list[dict]:
    """The tools to start for a task: turned on, and still exactly the files that were approved."""
    out = []
    data = load()
    changed = False
    for e in data["tools"].values():
        if e.get("status") != "enabled":
            continue
        try:
            same = files_hash(e["id"]) == e.get("approved_hash")
        except (OSError, ValueError):
            same = False
        if same:
            out.append(e)
        else:
            e["status"] = "changed"
            changed = True
            log(f"workshop tool {e.get('name', e['id'])} not loaded: its files changed after you approved it")
    if changed:
        save(data)
    return out


def server_params(tid: str):
    from mcp import StdioServerParameters
    return StdioServerParameters(command=python(), args=[str(HOST), str(folder(tid))], cwd=str(folder(tid)),
                                 env={"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})


def public() -> list[dict]:
    """For the Customize page: each tool, newest first, with whether its files still match what was approved."""
    out = []
    for e in sorted(load()["tools"].values(), key=lambda e: -e.get("created", 0)):
        try:
            current = files_hash(e["id"])
        except (OSError, ValueError):
            current = ""
        test = e.get("test") or {}
        out.append({**{k: e.get(k) for k in ("id", "name", "does", "why", "status", "created", "summary")},
                    "folder": str(folder(e["id"])), "tools": [f"{prefix(e['id'])}_{t['name']}" for t in test.get("tools") or []],
                    "tested": bool(test.get("ok")) and test.get("hash") == current,
                    "approved_same": bool(e.get("approved_hash")) and e.get("approved_hash") == current})
    return out


def remove(tid: str) -> None:
    """Its folder to the Recycle Bin (never deleted for good), and its entry gone."""
    d = folder(tid)
    if d.is_dir():
        q = str(d).replace("'", "''")
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Add-Type -AssemblyName Microsoft.VisualBasic; [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory('{q}', "
                        "'OnlyErrorDialogs', 'SendToRecycleBin')"], capture_output=True, timeout=60, creationflags=NO_WINDOW)
    data = load()
    data["tools"].pop(tid, None)
    save(data)
