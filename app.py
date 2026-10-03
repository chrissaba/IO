"""Local control panel for the boss agent: http://127.0.0.1:8765

Starts everything the agent needs (the boss model server, UI-TARS in Unsloth Studio),
then runs submitted tasks one after another, plus scheduled ones. History, schedules,
templates and settings are saved in data/store.json. Other programs can queue tasks too:

    POST http://127.0.0.1:8765/api/tasks   {"task": "open notepad and type hello"}
"""
import asyncio
import base64
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import uvicorn
from PIL import Image
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import boss
import learned
import nim
import overlay
import plugins
import triggers

HERE = Path(__file__).parent
PORT = int(os.environ.get("BOSS_APP_PORT", "8765"))
STUDIO_URL = "http://127.0.0.1:8888"
STUDIO_EXE = Path(os.environ.get("LOCALAPPDATA", "")) / "Unsloth Studio (Desktop)" / "unsloth-studio.exe"
STORE = HERE / "data" / "store.json"
UPLOADS = HERE / "data" / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 15 * 1024 * 1024
EYES_LOAD = {
    "model_path": boss.EYES_MODEL,
    "gguf_variant": "Q8_0",
    "max_seq_length": 32768,
    "n_parallel": 1,
    "speculative_type": "off",
}
MAX_EVENTS_PER_TASK = 600  # an Ultracode task logs for up to 5 helpers at once
MAX_HISTORY = 300
DEFAULT_SETTINGS = {
    "max_steps": 30, "allow_powershell": True, "notify": True, "hotkeys": True,
    "confirm_risky": True, "browser": True, "files": True, "watchdog": True, 
    "browser_mode": "edge", "model_mode": "fast", "focus_glow": True, "theme": "system",
    # the brain: ask_gemini on = the NVIDIA brain (GLM-5.3 Flash, DeepSeek V4.1 Flash, Kimi K3) runs tasks once a key is
    # saved; off = the local models alone. (The name is from when the remote AI was Gemini; boss.py and the bench read it.)
    "ask_gemini": True, "gemini_mode": "nim",
    "ultracode": False,  # by default: a plan first, then up to 5 helpers work on its parts at once (per message too)
}
ASK_TIMEOUT = 30 * 60  # how long a task waits for your answer before giving up on it

state: dict = {"tasks": [], "schedules": [], "templates": [], "triggers": [], "chats": [], "settings": dict(DEFAULT_SETTINGS)}
MAX_CHATS = 100
CHAT_CONTEXT_TURNS = 10  # earlier messages given to the agent with each new one
queue: asyncio.Queue = asyncio.Queue()
current: dict = {"task": None, "job": None}
status: dict = {"boss": "starting", "eyes": "starting", "paused": False}
# called with each finished task (the desktop app shows a notification)
finished_listeners: list = []
# called with each task that starts waiting on a question for you
question_listeners: list = []
answers: dict[str, asyncio.Future] = {}
folder_watch = triggers.FolderWatch()


# ---------- persistence ----------

def load_state() -> None:
    try:
        saved = json.loads(STORE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state["settings"]["brain_v2"] = True  # a fresh install: nothing to migrate
        return
    state.update({k: saved.get(k, v) for k, v in state.items()})
    state["settings"] = {**DEFAULT_SETTINGS, **state["settings"]}
    s = state["settings"]
    # from when IO had a cloud planner, then web chat AIs (Duck.ai, Gemini) as a director with a role and an order
    for old in ("planner_mode", "share_context", "advisor_role", "director_order"):
        s.pop(old, None)
    if s.get("gemini_mode") != "nim":  # no web chat AIs any more: the remote brain is NVIDIA's
        s["gemini_mode"] = "nim"
    if not s.get("brain_v2"):  # once: the NVIDIA brain is on whenever a key is saved (it used to hide behind a toggle)
        if nim.nim_key():
            s["ask_gemini"] = True
        s["brain_v2"] = True
    for task in state["tasks"]:  # anything mid-flight when the app closed didn't finish
        if task["status"] in ("queued", "running", "waiting"):
            task.update(status="cancelled", summary="app was closed")


def use_nim_once() -> bool:
    """The first key saved switches the NVIDIA brain on (once: turning it off in Settings afterwards sticks)."""
    s = state["settings"]
    if s.get("glm_switched") or not nim.nim_key():
        return False
    s["gemini_mode"], s["ask_gemini"], s["glm_switched"] = "nim", True, True
    return True


def save_state() -> None:
    STORE.parent.mkdir(exist_ok=True)
    # trim history oldest-first, but never tasks a chat still shows or anything still running
    keep = {m["task_id"] for c in state["chats"] for m in c["messages"]}
    extra = len(state["tasks"]) - MAX_HISTORY
    if extra > 0:
        drop = set()
        for t in state["tasks"]:
            if len(drop) >= extra:
                break
            if t["id"] not in keep and t["status"] not in ("queued", "running", "waiting"):
                drop.add(t["id"])
        dropped = [t for t in state["tasks"] if t["id"] in drop]
        state["tasks"] = [t for t in state["tasks"] if t["id"] not in drop]
        drop_uploads(dropped)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STORE)


def drop_uploads(removed: list[dict]) -> None:
    """Deletes the image files of removed tasks, unless another task still uses them (a re-run shares them)."""
    still = {n for t in state["tasks"] for n in t.get("images", [])}
    for t in removed:
        for n in t.get("images", []):
            if n not in still:
                (UPLOADS / n).unlink(missing_ok=True)


# ---------- model services ----------

def http_json(url: str, body: dict | None = None, timeout: float = 5, key: str = "") -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def boss_up() -> bool:
    try:
        return http_json(boss.BOSS_URL.removesuffix("/v1") + "/health").get("status") == "ok"
    except Exception:
        return False


BOSS_CMDS = {"fast": "start-boss-server.cmd", "smart": "start-qwen-server.cmd", "balanced": "start-qwen36-server.cmd"}
BOSS_NAMES = {"fast": "gemma", "smart": "qwen3.8", "balanced": "qwen3.6"}  # what the loaded model's file name contains


def boss_model_path() -> str:
    try:
        return str(http_json("http://127.0.0.1:8090/props", timeout=3).get("model_path", "")).lower()
    except Exception:
        return ""


def stop_boss_server() -> None:
    """Stops the llama-server on the boss port, so the other model can load."""
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" | Where-Object { $_.CommandLine -like '*--port 8090*' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"], creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)


switching = asyncio.Lock()


async def switch_models() -> None:
    """Fast <-> Smart: free the GPU first (UI-TARS out for Smart, Qwen out for Fast), then load the other."""
    async with switching:
        while current["task"] is not None:  # never pull the model out from under a running task
            await asyncio.sleep(2)
        mode = state["settings"].get("model_mode", "fast")
        status["boss"] = "switching model"
        if mode != "fast":
            await ensure_eyes()  # unloads UI-TARS
        await asyncio.to_thread(stop_boss_server)
        await asyncio.sleep(2)
        await ensure_boss()
        if mode == "fast":
            await ensure_eyes()


boss_starting = asyncio.Lock()


def boss_loading() -> bool:
    """A boss server is up but still loading its model (/health answers 503 'Loading model')."""
    try:
        urllib.request.urlopen(boss.BOSS_URL.removesuffix("/v1") + "/health", timeout=3)
        return False
    except urllib.error.HTTPError as e:
        return e.code == 503
    except Exception:
        return False


async def ensure_boss() -> None:
    # the watchdog and the task worker can both find the model down at once: start it once, and never start a second
    # server while one is still loading (two copies of a 21 GB model don't fit and both stall)
    async with boss_starting:
        await _ensure_boss()


async def _ensure_boss() -> None:
    mode = state["settings"].get("model_mode", "fast")
    if not boss_up() and await asyncio.to_thread(boss_loading):
        status["boss"] = "loading model"
        for _ in range(300):
            await asyncio.sleep(1)
            if boss_up():
                break
    if boss_up():
        path = await asyncio.to_thread(boss_model_path)
        if not path or BOSS_NAMES[mode] in path:
            status["boss"] = "ready"
            return
        await asyncio.to_thread(stop_boss_server)  # the other mode's model is loaded
        await asyncio.sleep(2)
    status["boss"] = {"smart": "starting Qwen 3.8 27B", "balanced": "starting Qwen 3.6 35B-A3B"}.get(mode, "starting model")
    log_file = open(HERE / "logs" / "boss-server.log", "a", encoding="utf-8")
    subprocess.Popen(
        ["cmd", "/c", str(HERE / BOSS_CMDS[mode])],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    for _ in range(300):  # Qwen 27B takes longer to load
        await asyncio.sleep(1)
        if boss_up():
            status["boss"] = "ready"
            return
    status["boss"] = "failed to start (see logs/boss-server.log)"


def studio_models(key: str) -> list[dict] | None:
    try:
        return http_json(f"{STUDIO_URL}/v1/models", key=key)["data"]
    except Exception:
        return None


async def ensure_eyes() -> None:
    """Makes sure UI-TARS is loaded in Unsloth Studio, opening Studio first if needed (e.g. at login).
    In Smart mode Qwen is its own eyes, so UI-TARS is unloaded instead to leave room on the GPU."""
    key = boss.studio_key()
    if state["settings"].get("model_mode", "fast") != "fast":
        try:
            await asyncio.to_thread(http_json, f"{STUDIO_URL}/v1/unload", {"model_path": boss.EYES_MODEL}, 60, key)
        except Exception:
            pass
        status["eyes"] = "ready"
        return
    models = studio_models(key)
    if models is None and STUDIO_EXE.exists():
        status["eyes"] = "opening Unsloth Studio"
        subprocess.Popen([str(STUDIO_EXE)], creationflags=subprocess.DETACHED_PROCESS)
    for _ in range(60):  # Studio can take a few minutes to come up after login
        if models is not None:
            break
        await asyncio.sleep(5)
        models = studio_models(key)
    if models is None:
        status["eyes"] = "waiting for Unsloth Studio"
        asyncio.create_task(retry_eyes_later())
        return
    if any(m["id"] == boss.EYES_MODEL and m.get("loaded") for m in models):
        status["eyes"] = "ready"
        return
    status["eyes"] = "loading UI-TARS in Studio"
    try:
        await asyncio.to_thread(http_json, f"{STUDIO_URL}/v1/load", {**EYES_LOAD, "force_reload": True}, 600, key)
        status["eyes"] = "ready"
    except Exception as e:
        status["eyes"] = f"couldn't load UI-TARS in Studio: {e}"


async def retry_eyes_later() -> None:
    await asyncio.sleep(30)
    await ensure_eyes()


# ---------- tasks ----------

def on_event(record: dict) -> None:
    task = current["task"]  # boss.log calls this under its own lock (helpers log from several threads at once)
    if task is not None:
        task["events"].append(record)
        if len(task["events"]) > MAX_EVENTS_PER_TASK:
            del task["events"][:-MAX_EVENTS_PER_TASK]


def new_task(text: str, source: str = "you", max_steps: int | None = None, chat_id: str = "", images: list[str] | None = None,
             task_id: str = "") -> dict:
    task = {
        "id": task_id or uuid.uuid4().hex[:8],
        "text": text,
        "source": source,
        "chat_id": chat_id,
        "max_steps": max_steps or state["settings"]["max_steps"],
        "status": "queued",
        "summary": "",
        "events": [],
        "created": time.time(),
    }
    if images:
        task["images"] = images
    state["tasks"].append(task)
    queue.put_nowait(task)
    save_state()
    return task


def asker(task: dict):
    """The ask callback for a task: shows the question in the app and waits for your answer."""

    async def ask(question: str) -> str:
        future = asyncio.get_running_loop().create_future()
        answers[task["id"]] = future
        task.update(status="waiting", question=question)
        save_state()
        for listener in question_listeners:
            try:
                listener(task)
            except Exception as e:
                print("question listener failed:", e)
        try:
            return await asyncio.wait_for(future, ASK_TIMEOUT)
        except asyncio.TimeoutError:
            return "(no answer; the user did not reply in time)"
        finally:
            answers.pop(task["id"], None)
            task.update(status="running", question="")

    return ask


def chat_context(task: dict) -> list[dict]:
    """Earlier turns of the task's chat: what you asked, and what the agent did and answered."""
    chat = next((c for c in state["chats"] if c["id"] == task.get("chat_id")), None)
    if not chat:
        return []
    by_id = {t["id"]: t for t in state["tasks"]}
    turns = []
    for m in chat["messages"]:
        if m.get("task_id") == task["id"]:
            break
        t = by_id.get(m.get("task_id"))
        if t is None:
            continue
        actions = [
            f"{e.get('name')}({json.dumps(e.get('args', {}), ensure_ascii=False)[:80]})"
            for e in t.get("events", []) if e.get("event") == "tool"
        ][-8:]
        n_img = len(t.get("images", []))
        asked = t["text"] + (f" [attached {n_img} image(s)]" if n_img else "")
        if actions:  # on the user's turn, not in the answer: a small model copies whatever its old answers look like
            asked += "\n(context, not part of the request: to answer this you used " + "; ".join(actions) + ")"
        turns.append({"role": "user", "content": asked})
        turns.append({"role": "assistant", "content": boss.clean_summary(t.get("summary") or "") or f"({t['status']})"})
    return turns[-CHAT_CONTEXT_TURNS * 2:]


def earlier_images(task: dict, turns: int = 2) -> list[str]:
    """Images from the last couple of turns of the task's chat, so "what does the second line say?" can still see them."""
    chat = next((c for c in state["chats"] if c["id"] == task.get("chat_id")), None)
    if not chat:
        return []
    by_id = {t["id"]: t for t in state["tasks"]}
    prev = []
    for m in chat["messages"]:
        if m.get("task_id") == task["id"]:
            break
        if m.get("task_id") in by_id:
            prev.append(by_id[m["task_id"]])
    return next((t["images"] for t in reversed(prev[-turns:]) if t.get("images")), [])


def error_text(e: BaseException) -> str:
    """The real error: the MCP clients' task groups wrap whatever went wrong as 'unhandled errors in a TaskGroup'."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        e = e.exceptions[0]
    print("task failed:", type(e).__name__, e, file=sys.stderr)
    return str(e) or type(e).__name__


async def worker() -> None:
    while True:
        task = await queue.get()
        while status["paused"] and task["status"] == "queued":
            await asyncio.sleep(1)
        if task["status"] == "cancelled":
            continue
        if not boss_up():  # the boss server may have been closed since startup
            await ensure_boss()
        task.update(status="running", started=time.time())
        current["task"] = task
        options = {k: state["settings"][k] for k in ("allow_powershell", "confirm_risky", "browser", "files", "browser_mode", "model_mode")}
        options["chrome_token"] = chrome_token()
        # the NVIDIA brain needs its key; without one IO runs on the local models (never a web chat AI)
        options["ask_gemini"] = bool(state["settings"].get("ask_gemini")) and bool(nim.nim_key())
        options["gemini_mode"], options["advisor_role"] = "nim", "director"
        # Ultracode: the message's own switch, else the Settings default (boss caps it at 5 helpers at once)
        options["ultracode"] = bool(task["ultracode"] if "ultracode" in task else state["settings"].get("ultracode"))
        options["focus_glow"] = bool(state["settings"].get("focus_glow", True))
        own = task.get("images") or []
        imgs = own or earlier_images(task)
        options["images_from_earlier"] = bool(imgs) and not own
        # a standing goal ("...until I tell you to stop", "/loop ...", or the loop button): runs until you press Stop
        options["loop"] = bool(task.get("loop") or boss.LOOP_REQUEST.search(task["text"]))
        if options["loop"]:
            task["loop"] = True
        if state["settings"].get("focus_glow", True):
            overlay.show()  # the purple glow around the window IO works in; screenshots never see it
        current["job"] = asyncio.create_task(
            boss.run(task["text"], task["max_steps"], options, asker(task), chat_context(task), [UPLOADS / n for n in imgs])
        )
        try:
            task["summary"] = await current["job"]
            task["status"] = "done"
        except asyncio.CancelledError:
            task["status"], task["summary"] = "cancelled", "stopped"
        except Exception as e:
            task["status"], task["summary"] = "error", error_text(e)
        task["finished"] = time.time()
        current.update(task=None, job=None)
        overlay.hide()
        save_state()
        for listener in finished_listeners:
            try:
                listener(task)
            except Exception as e:
                print("finished listener failed:", e)


def stop_all() -> None:
    """Emergency stop: cancel the running task and everything queued, and pause the queue."""
    status["paused"] = True
    for task in state["tasks"]:
        if task["status"] == "queued":
            task.update(status="cancelled", summary="stopped")
    if current["job"]:
        current["job"].cancel()
    save_state()


# ---------- schedules ----------

def next_run(s: dict, after: float) -> float:
    if s["kind"] == "every":
        return after + max(1, int(s["minutes"])) * 60
    hour, minute = (int(x) for x in s["at"].split(":"))
    t = datetime.fromtimestamp(after).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if t.timestamp() <= after:
        t += timedelta(days=1)
    return t.timestamp()


async def scheduler() -> None:
    while True:
        now = time.time()
        for s in state["schedules"]:
            if not s.get("enabled", True):
                continue
            if not s.get("next_run"):
                s["next_run"] = next_run(s, now)
            elif s["next_run"] <= now:
                busy = any(t["status"] in ("queued", "running", "waiting") and t.get("schedule") == s["id"] for t in state["tasks"])
                if not busy:  # don't pile up copies of a slow recurring task
                    new_task(s["text"], source=f"schedule: {s['name']}")["schedule"] = s["id"]
                s["last_run"], s["next_run"] = now, next_run(s, now)
                save_state()
        await asyncio.sleep(15)


# ---------- triggers ----------

async def trigger_loop() -> None:
    while True:
        for t in state["triggers"]:
            if t.get("enabled", True) and t["kind"] == "folder":
                try:
                    for f in folder_watch.new_files(t):
                        new_task(triggers.fill(t["template"], {"file": str(f), "name": f.name}), source=f"trigger: {t['name']}")
                    t["error"] = ""
                except Exception as e:
                    t["error"] = str(e)[:200]
        await asyncio.sleep(10)


async def fire_webhook(request: Request) -> JSONResponse:
    slug = request.path_params["slug"]
    t = next((x for x in state["triggers"] if x["kind"] == "webhook" and x.get("slug") == slug and x.get("enabled", True)), None)
    if t is None:
        return JSONResponse({"error": "no enabled webhook with that name"}, status_code=404)
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    values = {**(payload if isinstance(payload, dict) else {}), "payload": json.dumps(payload, ensure_ascii=False)}
    return JSONResponse({"id": new_task(triggers.fill(t["template"], values), source=f"webhook: {t['name']}")["id"]})


# ---------- watchdog ----------

async def watchdog() -> None:
    """Brings the boss model and UI-TARS back if they crash or get unloaded."""
    await asyncio.sleep(120)  # let startup finish first
    while True:
        if state["settings"].get("watchdog", True) and current["task"] is None:
            if not boss_up():
                print("watchdog: boss model down, restarting")
                await ensure_boss()
            models = studio_models(boss.studio_key())
            if state["settings"].get("model_mode", "fast") == "fast" and (
                    not models or not any(m["id"] == boss.EYES_MODEL and m.get("loaded") for m in models)):
                print("watchdog: UI-TARS not loaded, restoring")
                await ensure_eyes()
        await asyncio.sleep(30)


# ---------- app ----------

async def startup() -> None:
    (HERE / "logs").mkdir(exist_ok=True)
    boss.listeners.append(on_event)
    await asyncio.gather(ensure_boss(), ensure_eyes())


@asynccontextmanager
async def lifespan(_app):
    load_state()
    overlay.start(hint=lambda: boss.glow_hint())
    asyncio.create_task(startup())
    asyncio.create_task(worker())
    asyncio.create_task(scheduler())
    asyncio.create_task(trigger_loop())
    asyncio.create_task(watchdog())
    yield
    overlay.stop()
    save_state()


def visible_tasks() -> list[dict]:
    """The newest 100 tasks plus any older ones a chat still shows, newest first."""
    in_chats = {m["task_id"] for c in state["chats"] for m in c["messages"]}
    recent = state["tasks"][-100:]
    older = [t for t in state["tasks"][:-100] if t["id"] in in_chats]
    return (older + recent)[::-1]


def today_stats() -> dict:
    """Today's task count, successes and average time, over every task (the page only gets the newest 100)."""
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today = [t for t in state["tasks"] if (t.get("started") or t["created"]) >= midnight]
    fin = [t for t in today if t.get("finished") and t.get("started")]
    return {"count": len(today), "done": sum(t["status"] == "done" for t in today),
            "avg": round(sum(t["finished"] - t["started"] for t in fin) / len(fin)) if fin else 0}


async def get_state(_request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": status,
            "tasks": visible_tasks(),
            "chats": sorted(state["chats"], key=lambda c: c.get("updated", c["created"]), reverse=True),
            "schedules": state["schedules"],
            "templates": state["templates"],
            "triggers": [public_trigger(t) for t in state["triggers"]],
            "settings": state["settings"],
            "today": today_stats(),
            "chrome_token_set": bool(chrome_token()),
            "nim_key_set": bool(nim.nim_key()),  # never the key itself
            "brain_models": [nim.BRAIN_LABELS.get(m, m) for m in nim.BRAIN_MODELS],
            "learned": [{"name": k["name"], "runs": k.get("runs", 1), "uses": k.get("uses", 0), "playbook": k.get("playbook", ""),
                         "updated": k.get("updated", 0)} for k in sorted(learned.load(), key=lambda k: -k.get("updated", 0))],
            "user": os.environ.get("USERNAME", "").capitalize(),
        }
    )


async def new_chat(_request: Request) -> JSONResponse:
    chat = {"id": uuid.uuid4().hex[:8], "title": "New chat", "created": time.time(), "seen_at": time.time(), "messages": []}
    chats = state["chats"] + [chat]
    over = len(chats) - MAX_CHATS
    if over > 0:  # drop the least recently used unpinned chats, never pinned ones
        old = sorted((c for c in state["chats"] if not c.get("pinned")), key=lambda c: c.get("updated", c["created"]))
        victims = {c["id"] for c in old[:over]}
        chats = [c for c in chats if c["id"] not in victims]
    state["chats"] = chats
    save_state()
    return JSONResponse(chat)


async def chat_message(request: Request) -> JSONResponse:
    """Sends a message in a chat: it runs as a task that sees the chat's earlier turns."""
    chat = next((c for c in state["chats"] if c["id"] == request.path_params["id"]), None)
    if chat is None:
        return JSONResponse({"error": "no such chat"}, status_code=404)
    body = await request.json()
    text = str(body.get("text", "")).strip()
    images = [u for u in body.get("images", []) if isinstance(u, str) and u.startswith("data:image/")][:MAX_IMAGES]
    if not text and not images:
        return JSONResponse({"error": "text is required"}, status_code=400)
    release_questions(except_chat=chat["id"])
    task_id = uuid.uuid4().hex[:8]
    saved = save_images(task_id, images)
    if images and not saved:
        return JSONResponse({"error": "couldn't read the attached image(s)"}, status_code=400)
    task = new_task(text or "(see the attached image)", source="chat", chat_id=chat["id"], images=saved, task_id=task_id)
    if body.get("loop"):
        task["loop"] = True
    if "ultracode" in body:
        task["ultracode"] = bool(body["ultracode"])
    chat["messages"].append({"task_id": task["id"], "at": time.time()})
    # an image-only first message gets a placeholder name; the first message with text names the chat
    if chat["title"] in ("New chat", "Image", "Images") and text:
        chat["title"] = text[:60]
    elif chat["title"] == "New chat":
        chat["title"] = "Image" if len(saved) == 1 else "Images"
    chat["updated"] = time.time()
    save_state()
    return JSONResponse({"task_id": task["id"]})


async def pin_chat(request: Request) -> JSONResponse:
    chat = next((c for c in state["chats"] if c["id"] == request.path_params["id"]), None)
    if chat:
        chat["pinned"] = bool((await request.json()).get("pinned"))
        save_state()
    return JSONResponse({"ok": True})


async def chat_seen(request: Request) -> JSONResponse:
    """You've looked at this chat: replies finished before now stop counting as unread."""
    chat = next((c for c in state["chats"] if c["id"] == request.path_params["id"]), None)
    if chat:
        chat["seen_at"] = time.time()
        save_state()
    return JSONResponse({"ok": True})


def save_images(task_id: str, data_urls: list[str]) -> list[str]:
    """Stores pasted/dropped images under data/uploads and returns their file names. Each one is decoded first:
    common formats are kept as they are, others (TIFF, BMP...) become PNG, and unreadable ones are skipped."""
    UPLOADS.mkdir(parents=True, exist_ok=True)
    names = []
    for i, url in enumerate(data_urls):
        try:
            raw = base64.b64decode(url.partition(",")[2])
            if len(raw) > MAX_IMAGE_BYTES:
                continue
            with Image.open(io.BytesIO(raw)) as im:
                im.load()
                ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}.get(im.format)
                name = f"{task_id}-{i}.{ext or 'png'}"
                if ext:
                    (UPLOADS / name).write_bytes(raw)
                else:
                    im.convert("RGBA" if "A" in im.getbands() else "RGB").save(UPLOADS / name, "PNG")
            names.append(name)
        except Exception as e:
            print("skipped an attached image:", type(e).__name__, e)
    return names


async def rename_chat(request: Request) -> JSONResponse:
    chat = next((c for c in state["chats"] if c["id"] == request.path_params["id"]), None)
    if chat:
        chat["title"] = str((await request.json()).get("title", "")).strip()[:80] or chat["title"]
        save_state()
    return JSONResponse({"ok": True})


async def delete_chat(request: Request) -> JSONResponse:
    state["chats"] = [c for c in state["chats"] if c["id"] != request.path_params["id"]]
    save_state()
    return JSONResponse({"ok": True})


async def add_task(request: Request) -> JSONResponse:
    body = await request.json()
    text = str(body.get("task", "")).strip()
    if not text:
        return JSONResponse({"error": "task is required"}, status_code=400)
    # a re-run from History brings its images along
    images = [n for n in body.get("images", []) if isinstance(n, str) and re.fullmatch(r"[0-9a-f]{8}-\d\.(png|jpg|webp|gif)", n)
              and (UPLOADS / n).is_file()]
    return JSONResponse({"id": new_task(text, str(body.get("source", "you")), body.get("max_steps"), images=images)["id"]})


async def stop_task(request: Request) -> JSONResponse:
    task_id = request.path_params["id"]
    for task in state["tasks"]:
        if task["id"] == task_id:
            if task["status"] == "queued":
                task.update(status="cancelled", summary="stopped")
            elif task is current["task"] and current["job"]:
                current["job"].cancel()
    save_state()
    return JSONResponse({"ok": True})


async def clear_history(_request: Request) -> JSONResponse:
    removed = [t for t in state["tasks"] if t["status"] not in ("queued", "running", "waiting")]
    state["tasks"] = [t for t in state["tasks"] if t["status"] in ("queued", "running", "waiting")]
    drop_uploads(removed)
    save_state()
    return JSONResponse({"ok": True})


async def set_paused(request: Request) -> JSONResponse:
    status["paused"] = bool((await request.json()).get("paused"))
    return JSONResponse({"paused": status["paused"]})


async def stop_all_route(_request: Request) -> JSONResponse:
    stop_all()
    return JSONResponse({"ok": True})


async def save_schedule(request: Request) -> JSONResponse:
    body = await request.json()
    s = {
        "id": body.get("id") or uuid.uuid4().hex[:8],
        "name": str(body.get("name") or body.get("text", ""))[:60].strip(),
        "text": str(body.get("text", "")).strip(),
        "kind": "daily" if body.get("kind") == "daily" else "every",
        "minutes": int(body.get("minutes") or 60),
        "at": str(body.get("at") or "09:00"),
        "enabled": bool(body.get("enabled", True)),
    }
    if not s["text"]:
        return JSONResponse({"error": "text is required"}, status_code=400)
    s["next_run"] = next_run(s, time.time())
    state["schedules"] = [x for x in state["schedules"] if x["id"] != s["id"]] + [s]
    save_state()
    return JSONResponse(s)


async def delete_schedule(request: Request) -> JSONResponse:
    state["schedules"] = [s for s in state["schedules"] if s["id"] != request.path_params["id"]]
    save_state()
    return JSONResponse({"ok": True})


async def save_template(request: Request) -> JSONResponse:
    text = str((await request.json()).get("text", "")).strip()
    if text and text not in state["templates"]:
        state["templates"].append(text)
        save_state()
    return JSONResponse({"templates": state["templates"]})


async def delete_template(request: Request) -> JSONResponse:
    text = str((await request.json()).get("text", ""))
    state["templates"] = [t for t in state["templates"] if t != text]
    save_state()
    return JSONResponse({"templates": state["templates"]})


async def save_settings(request: Request) -> JSONResponse:
    body = await request.json()
    s = state["settings"]
    s["max_steps"] = max(5, min(100, int(body.get("max_steps", s["max_steps"]))))
    if body.get("theme") in ("system", "light", "dark"):
        s["theme"] = body["theme"]
    if body.get("browser_mode") in ("edge", "chrome"):
        s["browser_mode"] = body["browser_mode"]
    if body.get("model_mode") in ("fast", "smart", "balanced") and body["model_mode"] != s.get("model_mode"):
        s["model_mode"] = body["model_mode"]
        asyncio.create_task(switch_models())
    if "ask_gemini" in body:
        s["brain_v2"] = True  # the user chose: no migration ever flips it again
    for key in ("allow_powershell", "notify", "hotkeys", "confirm_risky", "browser", "files", "watchdog", "ask_gemini", "focus_glow", "ultracode"):
        if key in body:
            s[key] = bool(body[key])
    if not s["focus_glow"]:
        overlay.hide()
    elif current["task"]:
        overlay.show()
    save_state()
    return JSONResponse(s)


# ---------- your own Chrome (Playwright extension) ----------
BROWSER_FILE = HERE / "data" / "browser.json"


def chrome_token() -> str:
    try:
        return json.loads(BROWSER_FILE.read_text(encoding="utf-8")).get("chrome_token", "")
    except (OSError, ValueError):
        return ""


async def save_browser(request: Request) -> JSONResponse:
    """Stores the Playwright extension's token (from the extension's own page), so it connects without asking."""
    token = str((await request.json()).get("token", "")).strip()
    BROWSER_FILE.parent.mkdir(exist_ok=True)
    BROWSER_FILE.write_text(json.dumps({"chrome_token": token}), encoding="utf-8")
    return JSONResponse({"ok": True, "chrome_token_set": bool(token)})


async def save_nim_key(request: Request) -> JSONResponse:
    """Stores the NVIDIA API key for the brain (GLM-5.3 Flash, DeepSeek V4.1 Flash, Kimi K3) in data/nim_key.txt
    (git-ignored); an empty one clears it. The key is never sent back or logged: the page only learns whether one is set."""
    key = str((await request.json()).get("key", "")).strip()
    nim.save_nim_key(key)
    if use_nim_once():
        save_state()
    return JSONResponse({"ok": True, "nim_key_set": bool(key), "brain_on": bool(key) and bool(state["settings"].get("ask_gemini"))})


async def delete_learned(request: Request) -> JSONResponse:
    """Forgets one skill IO taught itself."""
    name = str((await request.json()).get("name", ""))
    return JSONResponse({"ok": learned.delete(name)})


async def test_nim_key(_request: Request) -> JSONResponse:
    """Checks the saved NVIDIA key with a free call (the model list): works (and whether all three brain models are
    offered to it), expired/invalid, or unreachable."""
    key = nim.nim_key()
    if not key:
        return JSONResponse({"ok": False, "result": "No key saved."})

    def check() -> str:
        try:
            req = urllib.request.Request(nim.NIM_URL + "/models", headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=20) as r:
                if r.status != 200:
                    return f"NVIDIA answered {r.status}."
                listed = {m.get("id") for m in json.load(r).get("data", []) if isinstance(m, dict)}
            missing = [nim.BRAIN_LABELS.get(m, m) for m in nim.BRAIN_MODELS if m not in listed]
            return "Works." if not missing else f"Works, but {', '.join(missing)} isn't offered to this key."
        except urllib.error.HTTPError as e:
            return "Expired or invalid: paste a new key." if e.code in (401, 403) else f"NVIDIA answered {e.code}."
        except Exception as e:
            return f"Couldn't reach NVIDIA: {type(e).__name__}"

    result = await asyncio.to_thread(check)
    return JSONResponse({"ok": result.startswith("Works"), "result": result})


EXTENSION_DIR = HERE / "chrome-extension"


async def open_extension_folder(_request: Request) -> JSONResponse:
    """Opens IO's Chrome extension folder in Explorer, for Chrome's "Load unpacked"."""
    os.startfile(EXTENSION_DIR)
    return JSONResponse({"ok": True, "path": str(EXTENSION_DIR)})


async def test_browser(_request: Request) -> JSONResponse:
    options = {"browser_mode": state["settings"].get("browser_mode", "edge"), "chrome_token": chrome_token()}
    try:
        result = await asyncio.wait_for(boss.test_browser(options), 90)
        return JSONResponse({"ok": True, "result": result[:600]})
    except Exception as e:
        return JSONResponse({"ok": False, "result": f"{type(e).__name__}: {e}"[:600]})


def release_questions(except_chat: str = "") -> None:
    """You moved on to something else: a task still waiting on your answer stops waiting (and reports what it has),
    so it doesn't hold up the queue."""
    for task in state["tasks"]:
        future = answers.get(task["id"])
        if task["status"] == "waiting" and task.get("chat_id") != except_chat and future and not future.done():
            future.set_result("(no answer: the user moved on to something else. Stop here and report what you have.)")


async def answer_task(request: Request) -> JSONResponse:
    future = answers.get(request.path_params["id"])
    if future is None or future.done():
        return JSONResponse({"error": "that task isn't waiting for an answer"}, status_code=409)
    future.set_result(str((await request.json()).get("answer", "")))
    return JSONResponse({"ok": True})


async def get_memory(_request: Request) -> JSONResponse:
    return JSONResponse({"notes": boss.memory_load()})


async def add_memory(request: Request) -> JSONResponse:
    boss.remember(str((await request.json()).get("text", "")))
    return JSONResponse({"notes": boss.memory_load()})


async def delete_memory(request: Request) -> JSONResponse:
    note_id = request.path_params["id"]
    boss.memory_save([n for n in boss.memory_load() if n["id"] != note_id])
    return JSONResponse({"notes": boss.memory_load()})


TRIGGER_FIELDS = ("name", "kind", "template", "folder", "pattern", "slug")


async def save_trigger(request: Request) -> JSONResponse:
    body = await request.json()
    old = next((t for t in state["triggers"] if t["id"] == body.get("id")), {})
    t = {**old, **{k: str(body.get(k) or "").strip() for k in TRIGGER_FIELDS}}
    t["id"] = old.get("id") or uuid.uuid4().hex[:8]
    t["enabled"] = bool(body.get("enabled", True))
    if t["kind"] not in ("folder", "webhook") or not t["template"]:
        return JSONResponse({"error": "kind and task template are required"}, status_code=400)
    if t["kind"] == "webhook":
        t["slug"] = re.sub(r"[^a-z0-9-]+", "-", (t["slug"] or t["name"]).lower()).strip("-") or t["id"]
    state["triggers"] = [x for x in state["triggers"] if x["id"] != t["id"]] + [t]
    save_state()
    return JSONResponse(public_trigger(t))


async def delete_trigger(request: Request) -> JSONResponse:
    state["triggers"] = [t for t in state["triggers"] if t["id"] != request.path_params["id"]]
    save_state()
    return JSONResponse({"ok": True})


def public_trigger(t: dict) -> dict:
    return dict(t)


async def get_customize(_request: Request) -> JSONResponse:
    return JSONResponse(plugins.public())


async def install_plugin(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        plugins.install(request.path_params["id"], bool(body.get("enabled", True)), body.get("env") or {})
    except KeyError:
        return JSONResponse({"error": "unknown plugin"}, status_code=404)
    return JSONResponse(plugins.public())


async def remove_plugin(request: Request) -> JSONResponse:
    plugins.uninstall(request.path_params["id"])
    return JSONResponse(plugins.public())


async def test_plugin(request: Request) -> JSONResponse:
    return JSONResponse({"result": await plugins.test(request.path_params["id"])})


async def save_skill(request: Request) -> JSONResponse:
    try:
        plugins.save_skill(await request.json())
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(plugins.public())


async def remove_skill(request: Request) -> JSONResponse:
    plugins.delete_skill(request.path_params["id"])
    return JSONResponse(plugins.public())


async def run_toolcheck(_request: Request) -> JSONResponse:
    """Runs toolcheck.py (a no-side-effects test of every tool) and returns its report lines."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable.replace("pythonw.exe", "python.exe"), str(HERE / "toolcheck.py"),
        cwd=HERE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"}, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=240)
    lines = [l for l in out.decode("utf-8", "replace").splitlines() if l[:4] in ("OK  ", "FAIL", "--  ") or l.startswith("Windows-MCP")]
    return JSONResponse({"lines": lines})


async def index(_request: Request) -> HTMLResponse:
    return HTMLResponse((HERE / "panel.html").read_text(encoding="utf-8"))


ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}


class SameOriginOnly:
    """Web pages on other sites can't send commands here (queue a task, change settings, install plugins)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] not in ("GET", "HEAD", "OPTIONS") and not scope["path"].startswith("/api/hook/"):
            origin = dict(scope["headers"]).get(b"origin")
            if origin is not None and origin.decode("latin-1") not in ALLOWED_ORIGINS:
                return await PlainTextResponse("forbidden", status_code=403)(scope, receive, send)
        await self.app(scope, receive, send)


app = Starlette(
    middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"]), Middleware(SameOriginOnly)],
    routes=[
        Route("/", index),
        Route("/api/state", get_state),
        Route("/api/tasks", get_state, methods=["GET"]),  # kept for older callers
        Route("/api/tasks", add_task, methods=["POST"]),
        Route("/api/chats", new_chat, methods=["POST"]),
        Route("/api/chats/{id}/messages", chat_message, methods=["POST"]),
        Route("/api/chats/{id}/rename", rename_chat, methods=["POST"]),
        Route("/api/chats/{id}/pin", pin_chat, methods=["POST"]),
        Route("/api/chats/{id}/seen", chat_seen, methods=["POST"]),
        Route("/api/chats/{id}", delete_chat, methods=["DELETE"]),
        Route("/api/tasks/{id}/stop", stop_task, methods=["POST"]),
        Route("/api/history/clear", clear_history, methods=["POST"]),
        Route("/api/pause", set_paused, methods=["POST"]),
        Route("/api/stop_all", stop_all_route, methods=["POST"]),
        Route("/api/schedules", save_schedule, methods=["POST"]),
        Route("/api/schedules/{id}", delete_schedule, methods=["DELETE"]),
        Route("/api/templates", save_template, methods=["POST"]),
        Route("/api/templates/delete", delete_template, methods=["POST"]),
        Route("/api/settings", save_settings, methods=["POST"]),
        Route("/api/browser", save_browser, methods=["POST"]),
        Route("/api/keys/nim", save_nim_key, methods=["POST"]),
        Route("/api/keys/nim/test", test_nim_key, methods=["POST"]),
        Route("/api/learned/delete", delete_learned, methods=["POST"]),
        Route("/api/browser/test", test_browser, methods=["POST"]),
        Route("/api/browser/folder", open_extension_folder, methods=["POST"]),
        Route("/api/toolcheck", run_toolcheck, methods=["POST"]),
        Route("/api/customize", get_customize, methods=["GET"]),
        Route("/api/plugins/{id}", install_plugin, methods=["POST"]),
        Route("/api/plugins/{id}", remove_plugin, methods=["DELETE"]),
        Route("/api/plugins/{id}/test", test_plugin, methods=["POST"]),
        Route("/api/skills", save_skill, methods=["POST"]),
        Route("/api/skills/{id}", remove_skill, methods=["DELETE"]),
        Route("/api/tasks/{id}/answer", answer_task, methods=["POST"]),
        Route("/api/memory", get_memory, methods=["GET"]),
        Route("/api/memory", add_memory, methods=["POST"]),
        Route("/api/memory/{id}", delete_memory, methods=["DELETE"]),
        Route("/api/triggers", save_trigger, methods=["POST"]),
        Route("/api/triggers/{id}", delete_trigger, methods=["DELETE"]),
        Route("/api/hook/{slug}", fire_webhook, methods=["POST"]),
        Mount("/static", StaticFiles(directory=HERE / "static"), name="static"),
        Mount("/uploads", StaticFiles(directory=UPLOADS), name="uploads"),
    ],
    lifespan=lifespan,
)


if __name__ == "__main__":
    if sys.stderr is None:  # pythonw has no console; keep output in a log file instead
        (HERE / "logs").mkdir(exist_ok=True)
        sys.stdout = sys.stderr = open(HERE / "logs" / "app.log", "a", encoding="utf-8", buffering=1)
    try:  # IO (or another copy) already serves this port: just open it
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/state", timeout=5)
        webbrowser.open(f"http://127.0.0.1:{PORT}")
        sys.exit(0)
    except OSError:
        pass
    if os.environ.get("BOSS_APP_NO_BROWSER") != "1":
        webbrowser.open(f"http://127.0.0.1:{PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
