"""Approvals that don't stop the work. When an unattended run (a goal, a schedule, a trigger) reaches something that needs
your OK, it doesn't wait for you: a risky command runs on a copy of its folder (the sandbox) and its output and file
changes wait in Approvals, where you apply them to the real folder or throw them away; any other risky action waits there
without running. The run carries on with everything else.

The sandbox is a copy of one folder, not a virtual machine: what a command does inside that folder is caught and shown
as changes; what it does elsewhere (other folders, the network, other programs) is not. That's why those commands still
need your OK to run for real, and why applying a sandbox run copies its files over instead of running it again."""
import hashlib
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

SANDBOXES = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".") / "io-sandbox"
MAX_COPY_BYTES = 300 * 1024 * 1024  # a bigger folder isn't copied: its command just waits for your OK
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache"}


def _fingerprints(root: Path) -> dict[str, str]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            p = Path(dirpath) / f
            try:
                st = p.stat()
                h = hashlib.sha1(p.read_bytes()).hexdigest() if st.st_size < 20 * 1024 * 1024 else f"{st.st_size}:{st.st_mtime_ns}"
            except OSError:
                continue
            out[str(p.relative_to(root))] = h
    return out


def _size(root: Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            try:
                total += (Path(dirpath) / f).stat().st_size
            except OSError:
                pass
            if total > MAX_COPY_BYTES:
                return total
    return total


def sandbox_run(command: str, folder: str, timeout: int = 120) -> dict:
    """Runs a command line (cmd.exe) on a copy of folder. Returns what happened: output, exit code, and the files it
    added, changed or deleted in the copy. 'error' is set when the folder couldn't be copied (too big, missing)."""
    src = Path(folder)
    if not src.is_dir():
        return {"error": f"{src} isn't a folder"}
    if _size(src) > MAX_COPY_BYTES:
        return {"error": f"{src} is over {MAX_COPY_BYTES // (1024 * 1024)} MB, too big to copy into a sandbox"}
    sid = uuid.uuid4().hex[:8]
    work = SANDBOXES / sid / src.name
    shutil.copytree(src, work, ignore=shutil.ignore_patterns(*SKIP_DIRS), symlinks=True)
    before = _fingerprints(work)
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    t0 = time.time()
    try:
        r = subprocess.run(f'cmd.exe /d /s /c "{command}"', cwd=str(work), env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, creationflags=0x08000000)
        code, raw = r.returncode, r.stdout
    except subprocess.TimeoutExpired as e:
        code, raw = None, e.stdout or b""
    try:
        output = raw.decode("utf-8")
    except UnicodeDecodeError:
        output = raw.decode("mbcs", errors="replace")
    after = _fingerprints(work)
    changes = ([{"path": p, "kind": "added"} for p in sorted(after.keys() - before.keys())] +
               [{"path": p, "kind": "changed"} for p in sorted(k for k in after.keys() & before.keys() if after[k] != before[k])] +
               [{"path": p, "kind": "deleted"} for p in sorted(before.keys() - after.keys())])
    return {"id": sid, "folder": str(src), "work": str(work), "command": command, "code": code,
            "output": output.replace("\r\n", "\n")[-20000:], "changes": changes, "secs": round(time.time() - t0, 1)}


def apply(run: dict) -> str:
    """Copies a sandbox run's changes into the real folder. Returns a short account of what was done."""
    work, dest = Path(run["work"]), Path(run["folder"])
    done = []
    for c in run.get("changes", []):
        target = dest / c["path"]
        if c["kind"] == "deleted":
            if target.is_file():
                target.unlink()
                done.append(f"deleted {c['path']}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(work / c["path"], target)
            done.append(f"{c['kind']} {c['path']}")
    discard(run)
    return "; ".join(done) or "nothing to change"


def discard(run: dict) -> None:
    shutil.rmtree(Path(run.get("work", "")).parent, ignore_errors=True) if run.get("work") else None


def summary(run: dict) -> str:
    """What the brain is told about a sandbox run."""
    ch = run.get("changes", [])
    listed = ", ".join(f"{c['kind']} {c['path']}" for c in ch[:12]) + (f" and {len(ch) - 12} more" if len(ch) > 12 else "")
    code = "timed out" if run.get("code") is None else f"exit code {run['code']}"
    return (f"ran in a sandbox copy of {run['folder']} ({code}); in the copy it {listed or 'changed no files'}.\n"
            f"Output:\n{run.get('output', '')[-6000:] or '(no output)'}")
