"""Local control panel for the boss agent: http://127.0.0.1:8765

Starts everything the agent needs (the boss model server, UI-TARS in Unsloth Studio),
then runs submitted tasks one after another, plus scheduled ones. History, schedules,
templates and settings are saved in data/store.json. Other programs can queue tasks too:

    POST http://127.0.0.1:8765/api/tasks   {"task": "open notepad and type hello"}
"""
import asyncio
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
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
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import approvals
import boss
import remote
import learned
import nim
import overlay
import plugins
import triggers
import webpush
import workshop

HERE = Path(__file__).parent
PORT = int(os.environ.get("BOSS_APP_PORT", "8765"))
STUDIO_URL = "http://127.0.0.1:8888"
STORE = HERE / "data" / "store.json"
UPLOADS = HERE / "data" / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_EVENTS_PER_TASK = 1500  # an Ultracode task logs for up to 5 helpers at once, plus a timing record per model call
MAX_HISTORY = 300
DEFAULT_SETTINGS = {
    "max_steps": 200, "allow_powershell": True, "notify": True, "hotkeys": True,
    "confirm_risky": True, "browser": True, "files": True, "watchdog": True, "keep_awake": True,
    "remote_access": False, "wake_url": "",
    "browser_mode": "edge", "focus_glow": True, "theme": "system",
    # how hard IO works, by default (a message can pick its own): low = everything on this PC; medium = the local model,
    # with hard tasks and goals going to the NVIDIA models; high = the NVIDIA brain runs the task; max = high plus
    # Ultracode helpers. Each level also sets how much the models reason (boss.EFFORT). Medium and up need a key.
    "effort": "high",
    "brain_models": list(nim.BRAIN_MODELS),  # the NVIDIA models the brain goes round, in order
    "vision_model": "",  # the one look_at_screen asks first ("" = the brain's first vision model)
    "helper_model": "",  # the one Ultracode helpers start on ("" = the brain's first)
    "race_width": 0,  # 2-5: the brain's step goes to this many models at once and the fastest answer wins (0 = in turn)
    "debug": False,  # show each message's debug timeline: every model call, screenshot and tool, with timings
}
ASK_TIMEOUT = 30 * 60  # how long a task waits for your answer before giving up on it

state: dict = {"tasks": [], "schedules": [], "templates": [], "triggers": [], "chats": [], "goals": [], "approvals": [],
               "settings": dict(DEFAULT_SETTINGS)}
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
    if not s.get("brain_v2"):  # once: the NVIDIA brain is on whenever a key is saved (it used to hide behind a toggle)
        if nim.nim_key():
            s["ask_gemini"] = True
        s["brain_v2"] = True
    if "effort" not in saved.get("settings", {}):  # from the local-model modes and the brain/Ultracode toggles to one scale
        s["effort"] = ("max" if s.get("ultracode") else "high") if s.get("ask_gemini") and nim.nim_key() else "low"
        s["max_steps"] = DEFAULT_SETTINGS["max_steps"]  # was the step count itself (30 by default); now a cap over each level's
    for old in ("model_mode", "ask_gemini", "gemini_mode", "ultracode"):
        s.pop(old, None)
    if s.get("effort") not in boss.EFFORT:
        s["effort"] = DEFAULT_SETTINGS["effort"]
    for task in state["tasks"]:  # anything mid-flight when the app closed didn't finish
        if task["status"] in ("queued", "running", "waiting"):
            task.update(status="cancelled", summary="app was closed")


def use_nim_once() -> bool:
    """The first key saved moves the default effort from Low to High (once: a level picked afterwards sticks)."""
    s = state["settings"]
    if s.get("glm_switched") or not nim.nim_key():
        return False
    if s.get("effort") == "low":
        s["effort"] = "high"
    s["glm_switched"] = True
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


BOSS_CMD = "start-balanced-server.cmd"  # Muse Glimmer 30B thinks (:8090), EvoCUA-8B sees and clicks (:8091)
BOSS_NAME = "glimmer"  # what the loaded model's file name contains


def evo_up() -> bool:
    """The eyes (EvoCUA on its own server) answer."""
    try:
        return http_json(boss.EVO_URL.removesuffix("/v1") + "/health", timeout=3).get("status") == "ok"
    except Exception:
        return False


def boss_model_path() -> str:
    try:
        return str(http_json("http://127.0.0.1:8090/props", timeout=3).get("model_path", "")).lower()
    except Exception:
        return ""


def stop_boss_server() -> None:
    """Stops the llama-servers on the boss and eyes ports (a half-started pair, or another model on the boss port)."""
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" | "
                    "Where-Object { $_.CommandLine -like '*--port 8090*' -or $_.CommandLine -like '*--port 8091*' } | "
                    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"], creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)


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
    if not boss_up() and await asyncio.to_thread(boss_loading):
        status["boss"] = "loading model"
        for _ in range(300):
            await asyncio.sleep(1)
            if boss_up():
                break
    if boss_up():
        path = await asyncio.to_thread(boss_model_path)
        if (not path or BOSS_NAME in path) and await asyncio.to_thread(evo_up):  # both servers, the right model
            status["boss"] = status["eyes"] = "ready"
            return
    await asyncio.to_thread(stop_boss_server)  # another model on the port, or half of the pair died (it would hold its memory twice)
    await asyncio.sleep(1)
    await asyncio.to_thread(free_studio_gpu)
    status["boss"] = "starting Muse Glimmer + EvoCUA"
    status["eyes"] = "starting EvoCUA"
    log_file = open(HERE / "logs" / "boss-server.log", "a", encoding="utf-8")
    subprocess.Popen(
        ["cmd", "/c", str(HERE / BOSS_CMD)],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    for _ in range(300):
        await asyncio.sleep(1)
        if boss_up() and await asyncio.to_thread(evo_up):
            status["boss"] = status["eyes"] = "ready"
            return
    failed = "failed to start (see logs/boss-server.log)"
    status["boss"] = "ready" if boss_up() else failed
    status["eyes"] = "ready" if await asyncio.to_thread(evo_up) else failed


def free_studio_gpu() -> None:
    """IO no longer uses Unsloth Studio's server (it hosted UI-TARS). If Studio happens to be open with a model loaded,
    that model would take the GPU memory Glimmer and EvoCUA need: unload it. Studio is never started from here."""
    key = boss.studio_key()
    try:
        models = http_json(f"{STUDIO_URL}/v1/models", key=key, timeout=3)["data"]
    except Exception:
        return
    for m in models:
        if m.get("loaded"):
            try:
                http_json(f"{STUDIO_URL}/v1/unload", {"model_path": m["id"]}, 60, key)
            except Exception:
                pass


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


def task_effort(task: dict) -> str:
    """The level a task runs at: its message's own pick, else the Settings default. A goal's check-ins get at least
    High from Medium up (goals are the long, unattended work the NVIDIA models are for). Without a key, everything is
    Low: IO never falls back to a web chat AI."""
    level = task.get("effort") if task.get("effort") in boss.EFFORT else state["settings"].get("effort", "high")
    if level not in boss.EFFORT:
        level = "high"
    if not nim.nim_key():
        return "low"
    if level == "medium" and (task.get("goal") or str(task.get("source", "")).startswith("goal: ")):
        return "high"
    return level


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
        options = {k: state["settings"][k] for k in ("allow_powershell", "confirm_risky", "browser", "files", "browser_mode")}
        options["chrome_token"] = chrome_token()
        options["effort"] = task["effort"] = task_effort(task)
        options["on_escalate"] = lambda level, t=task: t.update(effort=level, escalated=True)
        options["brain_models"] = list(state["settings"].get("brain_models") or nim.BRAIN_MODELS)
        options["vision_model"] = state["settings"].get("vision_model", "")
        options["race_width"] = int(state["settings"].get("race_width") or 0)
        options["helper_model"] = state["settings"].get("helper_model", "")
        options["focus_glow"] = bool(state["settings"].get("focus_glow", True))
        options["learn"] = task.get("learn", True)  # the benchmark sends learn=false: its runs teach IO nothing
        # nobody watches a goal's, schedule's or trigger's run: its risky steps wait in Approvals instead of stopping it
        options["unattended"] = bool(re.match(r"(schedule|trigger|webhook|goal): ", str(task.get("source", ""))))
        options["approve_later"] = approve_later_for(task)
        options["preapproved"] = list(task.get("preapproved") or [])
        options["add_goal"] = add_goal  # "keep an eye on X": the brain turns ongoing asks into goals
        options["workshop_propose"] = workshop_proposer(task)  # a missing ability: ask to build it (workshop.py)
        options["workshop_ready"] = workshop_ready_for(task)
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
        if task.get("goal"):
            goal_ran(task)
        current.update(task=None, job=None)
        overlay.hide()
        save_state()
        if wall_worthy(task):
            asyncio.create_task(wall_check_after(task))  # in the background: the next task doesn't wait for it
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


# ---------- goals: standing objectives IO checks on by itself ----------
# A goal is something to keep working toward ("keep my downloads folder sorted", "watch this repo and fix failing
# tests"), not a one-off task. Each has its own chat, so every check-in sees what the earlier ones did and found; a
# heartbeat queues the next check-in every few minutes; each run's answer becomes the goal's progress and a note; and the
# brain ends a goal by saying GOAL COMPLETE.

GOAL_PROMPT = """You are working on a standing goal, one check-in at a time: {objective}

Progress so far: {progress}
Recent notes:
{notes}

This is check-in #{n}. Look at where things stand now and do the next useful piece of work toward the goal (if nothing
needs doing, say so briefly). End with done: what you did or found, and what the next check-in should do. If the goal is
fully achieved and needs no more check-ins, start your done summary with GOAL COMPLETE."""


def goal_chat(goal: dict) -> dict:
    chat = next((c for c in state["chats"] if c["id"] == goal.get("chat_id")), None)
    if chat is None:
        chat = {"id": uuid.uuid4().hex[:8], "title": "Goal: " + goal["title"], "created": time.time(), "seen_at": time.time(),
                "messages": [], "pinned": True}
        state["chats"].append(chat)
        goal["chat_id"] = chat["id"]
    return chat


def check_goal(goal: dict, extra: str = "") -> dict:
    """Queues one check-in for a goal, in its chat."""
    notes = "\n".join(f"- {datetime.fromtimestamp(n['t']).strftime('%b %d %H:%M')}: {n['text'][:300]}" for n in goal.get("notes", [])[-8:])
    text = GOAL_PROMPT.format(objective=goal["objective"], progress=goal.get("progress") or "(first check-in)",
                              notes=notes or "(none yet)", n=goal.get("runs", 0) + 1) + (f"\n\nWhat prompted this check-in: {extra}" if extra else "")
    chat = goal_chat(goal)
    task = new_task(text, source=f"goal: {goal['title']}", chat_id=chat["id"])
    task["goal"] = goal["id"]
    chat["messages"].append({"task_id": task["id"], "at": time.time()})
    chat["updated"] = time.time()
    goal["next_check"] = time.time() + max(5, int(goal.get("every_minutes") or 30)) * 60
    save_state()
    return task


def goal_ran(task: dict) -> None:
    goal = next((g for g in state["goals"] if g["id"] == task["goal"]), None)
    if goal is None:
        return
    summary = boss.clean_summary(task.get("summary") or "") or task["status"]
    goal["runs"] = goal.get("runs", 0) + 1
    goal["last_run"] = time.time()
    goal.setdefault("notes", []).append({"t": time.time(), "text": summary[:1200], "status": task["status"]})
    goal["notes"] = goal["notes"][-40:]
    if task["status"] == "done":
        goal["progress"] = summary[:2000]
        if re.match(r"\s*GOAL COMPLETE", summary, re.I):
            goal["status"] = "done"
    goal["next_check"] = time.time() + max(5, int(goal.get("every_minutes") or 30)) * 60


async def goal_loop() -> None:
    """The heartbeat: each active goal gets a check-in when it's due (never two at once for the same goal)."""
    await asyncio.sleep(20)
    while True:
        now = time.time()
        for g in state["goals"]:
            if g.get("status", "active") != "active" or (g.get("next_check") or 0) > now:
                continue
            if any(t["status"] in ("queued", "running", "waiting") and t.get("goal") == g["id"] for t in state["tasks"]):
                continue
            check_goal(g)
        await asyncio.sleep(20)


def add_goal(objective: str, every_minutes: int = 30, title: str = "") -> dict:
    goal = {"id": uuid.uuid4().hex[:8], "title": (title or objective)[:60], "objective": objective.strip(),
            "every_minutes": max(5, int(every_minutes or 30)), "status": "active", "created": time.time(),
            "next_check": time.time(), "runs": 0, "progress": "", "notes": []}
    state["goals"].append(goal)
    goal_chat(goal)
    save_state()
    return goal


async def save_goal(request: Request) -> JSONResponse:
    body = await request.json()
    if body.get("id"):
        goal = next((g for g in state["goals"] if g["id"] == body["id"]), None)
        if goal is None:
            return JSONResponse({"error": "no such goal"}, status_code=404)
        for k in ("title", "objective", "every_minutes", "status"):
            if k in body:
                goal[k] = body[k]
        if body.get("status") == "active" and not goal.get("next_check"):
            goal["next_check"] = time.time()
        save_state()
        return JSONResponse(goal)
    if not str(body.get("objective", "")).strip():
        return JSONResponse({"error": "objective is required"}, status_code=400)
    return JSONResponse(add_goal(body["objective"], body.get("every_minutes", 30), body.get("title", "")))


async def goal_action(request: Request) -> JSONResponse:
    """check (a check-in now, optionally with what prompted it) or delete."""
    goal = next((g for g in state["goals"] if g["id"] == request.path_params["id"]), None)
    if goal is None:
        return JSONResponse({"error": "no such goal"}, status_code=404)
    action = request.path_params["action"]
    if action == "delete":
        state["goals"] = [g for g in state["goals"] if g["id"] != goal["id"]]
        save_state()
        return JSONResponse({"ok": True})
    try:
        body = await request.json()
    except ValueError:
        body = {}
    return JSONResponse({"task_id": check_goal(goal, str(body.get("event") or body.get("payload") or "")[:2000])["id"]})


# ---------- approvals that don't stop the work ----------

def approve_later_for(task: dict):
    """The callback boss uses for a risky step in an unattended run: the step waits in Approvals (a risky command runs on
    a sandbox copy of its folder first) and the brain is told so, then carries on."""

    async def approve_later(name: str, args: dict, reason: str) -> str:
        item = {"id": uuid.uuid4().hex[:6], "created": time.time(), "task_id": task["id"], "goal": task.get("goal", ""),
                "source": task.get("source", ""), "name": name, "args": args, "reason": reason, "status": "pending"}
        text = (f"Not done yet: this needs the user's OK ({reason}), and nobody is watching this run. It waits in Approvals as "
                f"#{item['id']}. Carry on with whatever doesn't depend on it, and mention it in done.")
        if name == "run_command" and args.get("command"):
            folder = str(args.get("folder") or approvals.folder_of(str(args["command"])) or os.path.expanduser("~"))
            run = await asyncio.to_thread(approvals.sandbox_run, str(args["command"]), folder, int(args.get("timeout") or 120))
            if "error" not in run:
                item["sandbox"] = run
                text = (f"Not run for real yet: this needs the user's OK ({reason}). Instead it {approvals.summary(run)}\n"
                        f"Those changes wait in Approvals as #{item['id']}; carry on with the rest and mention it in done.")
            else:
                item["sandbox_error"] = run["error"]
        state["approvals"].append(item)
        state["approvals"] = state["approvals"][-200:]
        save_state()
        for listener in question_listeners:  # the desktop app's notification: something waits for you
            try:
                listener({**task, "question": f"Approval needed: {reason}", "approval": item["id"]})
            except Exception as e:
                print("question listener failed:", e)
        return text

    return approve_later


def add_approval(item: dict, task: dict) -> None:
    """Something waits for the user's OK: kept in Approvals, and the desktop and phones are told."""
    state["approvals"].append(item)
    state["approvals"] = state["approvals"][-200:]
    save_state()
    for listener in question_listeners:
        try:
            listener({**task, "question": f"Approval needed: {item['reason']}", "approval": item["id"]})
        except Exception as e:
            print("question listener failed:", e)


# ---------- the workshop: tools IO builds for itself (workshop.py) ----------

def propose_tool_now(task: dict, name: str, does: str, why: str, source: str) -> str:
    """A missing ability becomes "IO can't do X yet; may it build a tool that does Y?" in Approvals (once per idea)."""
    entry, new = workshop.propose(name, does, why, task_id=task.get("id", ""), source=source)
    if not new:
        st = entry.get("status")
        if st == "enabled":
            return f"IO already has a tool for this, {entry['name']}: its tools are named {workshop.prefix(entry['id'])}_..."
        if st == "declined":
            return f"The user declined a tool like this ({entry['name']}) recently: don't ask again; do what you can without it."
        return f"A tool like this ({entry['name']}) is already {st}; nothing new to ask. Do what you can without it."
    item = {"id": uuid.uuid4().hex[:6], "created": time.time(), "task_id": task.get("id", ""), "goal": task.get("goal", ""),
            "source": source, "name": "build_tool", "status": "pending", "reason": f"build a new tool: {entry['name']}",
            "args": {"tool": entry["id"], "name": entry["name"], "does": entry["does"], "why": entry["why"]}}
    add_approval(item, task)
    return (f"Asked the user whether IO may build \"{entry['name']}\" in its workshop (Approvals #{item['id']}). Carry on "
            "with what you can without it, and mention it in done.")


def workshop_proposer(task: dict):
    async def propose(name: str, does: str, why: str) -> str:
        return propose_tool_now(task, name, does, why, source="asked by IO")
    return propose


def workshop_ready_for(task: dict):
    async def ready(tid: str, summary: str, files: str) -> str:
        entry = workshop.update(tid, status="built", summary=summary[:600])
        test = entry.get("test") or {}
        item = {"id": uuid.uuid4().hex[:6], "created": time.time(), "task_id": task["id"], "source": "workshop",
                "name": "enable_tool", "status": "pending", "reason": f"turn on IO's new tool: {entry.get('name', tid)}",
                "args": {"tool": tid, "name": entry.get("name", tid), "does": entry.get("does", ""), "summary": summary[:600],
                         "hash": files, "folder": str(workshop.folder(tid)), "test": (test.get("output") or "")[-1500:],
                         "tools": [f"{workshop.prefix(tid)}_{t['name']}{t['signature']}" for t in test.get("tools") or []]}}
        add_approval(item, task)
        return (f"Handed to the user to turn on (Approvals #{item['id']}). In done, say what the tool does and that it waits "
                "for their OK in Approvals.")
    return ready


def wall_worthy(task: dict) -> bool:
    """A task worth asking "was a missing tool the wall?" about: one that didn't get done, and isn't a loop, a workshop
    build, or a task that already proposed a tool."""
    if task.get("loop") or task.get("workshop_build") or task.get("source") in ("workshop", "approval") or task.get("learn") is False:
        return False
    events = task.get("events", [])
    if any(e.get("event") == "tool" and e.get("name") == "propose_tool" for e in events):
        return False
    if task["status"] == "error":
        return True
    # done, but only by improvising: a tool said it can't ("no PDF reader") and a script did it instead. The brain was
    # told to propose a tool then, and didn't (it answered and stopped), so this asks for it
    tools = [e for e in events if e.get("event") == "tool"]
    if any(str(e.get("result", "")).startswith("error:UNSUPPORTED") for e in tools) and any(
            e.get("name") in ("PowerShell", "run_command") and not str(e.get("result", "")).startswith("error") for e in tools):
        return True
    return task["status"] == "done" and bool(CANT.search(boss.clean_summary(task.get("summary") or "")[:800]))


# an answer that says IO couldn't do something (boss.NOT_DONE's "would" and "instead of" are too common in good answers)
CANT = re.compile(r"\b(couldn'?t|could not|can'?t|cannot|unable to|not able to|no way to|wasn'?t able|don'?t have (?:a|any) (?:tool|way))\b", re.I)


async def wall_check_after(task: dict) -> None:
    steps = [f"{e.get('name')}({json.dumps(e.get('args', {}), ensure_ascii=False)[:120]}) -> {str(e.get('result', ''))[:160]}"
             for e in task.get("events", []) if e.get("event") == "tool"]
    try:
        found = await asyncio.to_thread(boss.wall_check, task["text"], task.get("summary") or "", steps)
    except Exception as e:
        print("wall check failed:", e, file=sys.stderr)
        return
    if found:
        propose_tool_now(task, found["name"], found["does"], found["why"], source="wall check")


def workshop_decision(item: dict, decision: str) -> None:
    """build_tool approved: a task builds and tests it in the workshop. enable_tool approved: it's on from the next task,
    pinned to exactly the files that were tested."""
    tid = item["args"]["tool"]
    entry = workshop.get(tid)
    item["decided"] = time.time()
    if entry is None:
        item["status"], item["result"] = "failed", "that tool was removed"
        return
    if decision == "reject":
        item["status"] = "rejected"
        workshop.update(tid, status="declined" if item["name"] == "build_tool" else "built", decided=time.time())
        return
    if item["name"] == "build_tool":
        origin = next((t for t in state["tasks"] if t["id"] == item["task_id"]), {})
        t = new_task(workshop.build_text(entry), source="workshop", chat_id=origin.get("chat_id", ""))
        t["effort"] = "high"  # writing and testing code is the NVIDIA brain's work (without a key, task_effort makes it Low)
        t["workshop_build"] = tid
        chat = next((c for c in state["chats"] if c["id"] == origin.get("chat_id")), None)
        if chat:
            chat["messages"].append({"task_id": t["id"], "at": time.time()})
        workshop.update(tid, status="building", task_id=t["id"], decided=time.time())
        item["status"], item["result"] = "approved", f"building it as task {t['id']}"
        return
    now = workshop.files_hash(tid)
    test = entry.get("test") or {}
    if now != item["args"].get("hash") or not test.get("ok") or test.get("hash") != now:
        item["status"], item["result"] = "failed", "its files changed after the test: ask IO to test it again"
        return
    workshop.update(tid, status="enabled", approved_hash=now, enabled_at=time.time())
    item["status"], item["result"] = "applied", "on from the next task"


async def workshop_action(request: Request) -> JSONResponse:
    """Settings for a tool IO built: off, on (only as approved: same files), open its folder, remove (Recycle Bin)."""
    tid = request.path_params["id"]
    entry = workshop.get(tid)
    if entry is None:
        return JSONResponse({"error": "no such tool"}, status_code=404)
    act = (await request.json()).get("action")
    if act == "off":
        workshop.update(tid, status="off")
    elif act == "on":
        if not entry.get("approved_hash") or entry["approved_hash"] != await asyncio.to_thread(workshop.files_hash, tid):
            return JSONResponse({"error": "its files aren't the ones you approved: ask IO to test it again, then approve it"}, status_code=409)
        workshop.update(tid, status="enabled")
    elif act == "open":
        d = workshop.folder(tid)
        if d.is_dir():
            os.startfile(d)
    elif act == "remove":
        await asyncio.to_thread(workshop.remove, tid)
    else:
        return JSONResponse({"error": "action must be on, off, open or remove"}, status_code=400)
    return JSONResponse({"workshop": workshop.public()})


async def decide_approval(request: Request) -> JSONResponse:
    """approve: a sandbox run's changes are copied into the real folder; any other step runs as a new task, in the same
    chat, allowed to do exactly that one thing. reject: discarded."""
    item = next((a for a in state["approvals"] if a["id"] == request.path_params["id"]), None)
    if item is None or item["status"] != "pending":
        return JSONResponse({"error": "no pending approval with that id"}, status_code=404)
    decision = (await request.json()).get("decision")
    if item["name"] in ("build_tool", "enable_tool"):
        if remote.is_remote(request.scope):
            return JSONResponse({"error": "IO's own new tools are approved on the PC"}, status_code=403)
        if decision not in ("approve", "reject"):
            return JSONResponse({"error": "decision must be approve or reject"}, status_code=400)
        workshop_decision(item, decision)
        save_state()
        return JSONResponse(item)
    item["decided"] = time.time()
    if decision == "reject":
        item["status"] = "rejected"
        if item.get("sandbox"):
            await asyncio.to_thread(approvals.discard, item["sandbox"])
    elif decision == "approve":
        if item.get("sandbox"):
            try:
                item["result"] = await asyncio.to_thread(approvals.apply, item["sandbox"])
                item["status"] = "applied"
            except OSError as e:
                item["status"], item["result"] = "failed", str(e)
        else:
            origin = next((t for t in state["tasks"] if t["id"] == item["task_id"]), {})
            text = (f"Earlier, while working on this, you wanted to {item['reason']} ({item['name']} with "
                    f"{json.dumps(item['args'], ensure_ascii=False)[:600]}), and the user has now approved it. Do exactly that, "
                    "check it worked, and say so in done.")
            t = new_task(text, source="approval", chat_id=origin.get("chat_id", ""))
            t["preapproved"] = [item["reason"]]
            if item.get("goal"):
                t["goal"] = item["goal"]  # it's that goal's work: its answer updates the goal (and can complete it)
            chat = next((c for c in state["chats"] if c["id"] == origin.get("chat_id")), None)
            if chat:
                chat["messages"].append({"task_id": t["id"], "at": time.time()})
            item["status"], item["result"] = "approved", f"running as task {t['id']}"
    else:
        return JSONResponse({"error": "decision must be approve or reject"}, status_code=400)
    save_state()
    return JSONResponse(item)


# ---------- keep awake while there is work ----------

async def keep_awake() -> None:
    """While anything is queued, running or waiting, or a goal's check-in is due within 10 minutes, Windows isn't allowed
    to sleep from idleness (the PC sleeping paused a long benchmark twice). The screen may still turn off; closing the
    lid or choosing Sleep still sleeps."""
    import ctypes
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    held = False
    while True:
        now = time.time()
        busy = any(t["status"] in ("queued", "running", "waiting") for t in state["tasks"]) or any(
            g.get("status", "active") == "active" and (g.get("next_check") or 0) - now < 600 for g in state["goals"]) or (
            now - remote.last_remote[0] < 900)  # your phone was here in the last 15 min (it may have just woken the PC)
        want = busy and state["settings"].get("keep_awake", True)
        if want != held:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if want else 0))
            held = want
        await asyncio.sleep(30)


# ---------- watchdog ----------

async def watchdog() -> None:
    """Brings Glimmer and EvoCUA back if either crashes."""
    await asyncio.sleep(120)  # let startup finish first
    while True:
        if state["settings"].get("watchdog", True) and current["task"] is None:
            if not boss_up() or (not evo_up() and not boss_loading()):
                print("watchdog: a local model is down, restarting")
                await ensure_boss()
        await asyncio.sleep(30)


# ---------- app ----------

async def startup() -> None:
    (HERE / "logs").mkdir(exist_ok=True)
    boss.listeners.append(on_event)
    await ensure_boss()


@asynccontextmanager
async def lifespan(_app):
    load_state()
    overlay.start(hint=lambda: boss.glow_hint())
    asyncio.create_task(startup())
    asyncio.create_task(worker())
    asyncio.create_task(scheduler())
    asyncio.create_task(trigger_loop())
    asyncio.create_task(watchdog())
    asyncio.create_task(goal_loop())
    asyncio.create_task(keep_awake())
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


LITE_EVENTS = {"tool", "todo", "plan", "progress", "warning", "director", "agents", "agent_done", "check", "done"}


def lite_task(t: dict, with_events: bool) -> dict:
    """A task for a phone: its checklist's events only (the last 80, results cut short), or none for older tasks."""
    out = {k: v for k, v in t.items() if k != "events"}
    if with_events:
        evs = [e for e in t.get("events", []) if e.get("event") in LITE_EVENTS][-80:]
        cut = lambda v: v[:200] + "…" if isinstance(v, str) and len(v) > 200 else v  # a file's whole text is a write's argument
        out["events"] = [{**e, **({"result": str(e["result"])[:300]} if "result" in e else {}),
                          **({"args": {k: cut(v) for k, v in e["args"].items()}} if isinstance(e.get("args"), dict) else {})} for e in evs]
    else:
        out["events"] = []
    return out


async def get_state(request: Request) -> Response:
    """Everything the panel shows. ?lite=1 (the phone): only the newest tasks keep their checklist events, so a poll is
    a few KB instead of 1.7 MB; and an unchanged state answers 304 to the ETag it sent."""
    lite = bool(request.query_params.get("lite"))
    tasks = visible_tasks()
    if lite:
        tasks = [lite_task(t, i < 25 or t["status"] in ("queued", "running", "waiting")) for i, t in enumerate(tasks)]
    body = state_body(tasks, lite)
    if not lite:
        return JSONResponse(body)
    raw = json.dumps(body, ensure_ascii=False).encode()
    etag = '"' + hashlib.sha1(raw).hexdigest()[:20] + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(raw, media_type="application/json", headers={"ETag": etag, "Cache-Control": "no-cache"})


def state_body(tasks: list, lite: bool) -> dict:
    return (
        {
            "status": status,
            "tasks": tasks,
            "remote": {"devices": remote.devices(), "enabled": bool(state["settings"].get("remote_access"))} if not lite else {},
            "chats": sorted(state["chats"], key=lambda c: c.get("updated", c["created"]), reverse=True),
            "schedules": state["schedules"],
            "goals": state["goals"],
            # what waits for you, then the latest decided ones (a sandbox run's output and its list of changed files)
            "approvals": [a for a in state["approvals"] if a["status"] == "pending"] + [a for a in state["approvals"] if a["status"] != "pending"][-20:],
            "templates": state["templates"],
            "triggers": [public_trigger(t) for t in state["triggers"]],
            "settings": state["settings"],
            "today": today_stats(),
            "chrome_token_set": bool(chrome_token()),
            "nim_key_set": bool(nim.nim_key()),  # never the key itself
            "brain_models": [nim.label(m) for m in state["settings"].get("brain_models") or nim.BRAIN_MODELS],
            "model_info": {m: {"label": nim.label(m), "vision": nim.is_vision(m), **{k: v for k, v in nim.tests().get(m, {}).items() if k in ("tools", "secs", "note", "when")}}
                           for m in dict.fromkeys(list(state["settings"].get("brain_models") or nim.BRAIN_MODELS) + list(nim.tests()))},
            "learned": [{"name": k["name"], "runs": k.get("runs", 1), "uses": k.get("uses", 0), "playbook": k.get("playbook", ""),
                         "updated": k.get("updated", 0)} for k in sorted(learned.load(), key=lambda k: -k.get("updated", 0))],
            "user": os.environ.get("USERNAME", "").capitalize(),
            "machine": os.environ.get("COMPUTERNAME", ""),
        }
    )


# what a click in an answer may open with its own app; everything else is shown selected in File Explorer, so a click
# never runs a program or script
OPEN_SAFE = {".txt", ".md", ".csv", ".json", ".log", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".pdf", ".html", ".htm",
             ".xml", ".yaml", ".yml", ".docx", ".xlsx", ".pptx", ".mp4", ".mp3", ".wav"}


async def existing_paths(request: Request) -> JSONResponse:
    """For path-looking text in an answer ("C:\\...\\io-bench3 and started the server"), the longest leading part that
    exists on this PC, cut at a space: paths can contain spaces (old onedrive\\Documents), so only the disk can tell."""
    out = {}
    for cand in [str(c) for c in (await request.json()).get("candidates", [])][:60]:
        words, found = cand.rstrip().split(" "), ""
        for n in range(len(words), 0, -1):
            p = " ".join(words[:n]).rstrip(".,;:)!?'\"`")
            if len(p) > 3 and await asyncio.to_thread(os.path.exists, p):
                found = p
                break
            if n < len(words) - 12:
                break
        out[cand] = {"path": found, "dir": bool(found) and os.path.isdir(found)}
    return JSONResponse(out)


async def open_target(request: Request) -> JSONResponse:
    """A link or a path clicked in an answer: web addresses in the default browser, folders in File Explorer, documents in
    their app, anything else (programs, scripts) shown selected in File Explorer instead of run."""
    body = await request.json()
    target = str(body.get("target", "")).strip().strip("\"'`")
    if re.match(r"(?i)^https?://\S+$", target):
        await asyncio.to_thread(os.startfile, target)
        return JSONResponse({"ok": True, "how": "browser"})
    p = Path(os.path.expandvars(target.rstrip(".,;:)")))
    if not p.exists():
        return JSONResponse({"error": f"{p} doesn't exist (any more)"}, status_code=404)
    if p.is_dir():
        await asyncio.to_thread(os.startfile, str(p))
        how = "folder"
    elif p.suffix.lower() in OPEN_SAFE and not body.get("reveal"):
        await asyncio.to_thread(os.startfile, str(p))
        how = "app"
    else:
        subprocess.Popen(["explorer.exe", f"/select,{p}"])
        how = "explorer"
    return JSONResponse({"ok": True, "how": how})


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
    if remote.is_remote(request.scope):
        task["from_phone"] = True  # its answer is sent to the phone as a notification
    if body.get("loop"):
        task["loop"] = True
    if body.get("effort") in boss.EFFORT:
        task["effort"] = body["effort"]
    elif body.get("ultracode") is True:  # older callers: Ultracode on is the Max level
        task["effort"] = "max"
    if body.get("learn") is False:
        task["learn"] = False
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
    task = new_task(text, str(body.get("source", "you")), body.get("max_steps"), images=images)
    if body.get("effort") in boss.EFFORT:
        task["effort"] = body["effort"]
    if remote.is_remote(request.scope):
        task["from_phone"] = True
    return JSONResponse({"id": task["id"]})


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
    s["max_steps"] = max(5, min(200, int(body.get("max_steps", s["max_steps"]))))  # the cap; each level has its own steps
    if body.get("theme") in ("system", "light", "dark"):
        s["theme"] = body["theme"]
    if body.get("browser_mode") in ("edge", "chrome"):
        s["browser_mode"] = body["browser_mode"]
    if body.get("effort") in boss.EFFORT:
        s["effort"] = body["effort"]
    if isinstance(body.get("brain_models"), list):
        picked = [str(m).strip() for m in body["brain_models"] if str(m).strip()][:12]
        s["brain_models"] = list(dict.fromkeys(picked)) or list(nim.BRAIN_MODELS)
    if "race_width" in body:
        try:
            w = int(body["race_width"] or 0)
        except (TypeError, ValueError):
            w = 0
        s["race_width"] = 0 if w < 2 else min(w, nim.MAX_PARALLEL)
    for key in ("vision_model", "helper_model"):
        if key in body:
            s[key] = str(body[key] or "").strip()[:120]
    for key in ("allow_powershell", "notify", "hotkeys", "confirm_risky", "browser", "files", "watchdog", "focus_glow", "debug",
                "keep_awake", "remote_access"):
        if key in body:
            s[key] = bool(body[key])
    if "wake_url" in body:  # Home Assistant's webhook that sends this PC a wake packet (the phone calls it when IO is asleep)
        url = str(body["wake_url"] or "").strip()
        s["wake_url"] = url if re.match(r"^https?://\S+$", url) else ""
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
    return JSONResponse({"ok": True, "nim_key_set": bool(key), "effort": state["settings"].get("effort")})


async def nim_catalog(_request: Request) -> JSONResponse:
    """The chat models NVIDIA offers the saved key, for Settings' model picker."""
    if not nim.nim_key():
        return JSONResponse({"models": [], "error": "No key saved."})
    try:
        ids = await asyncio.to_thread(nim.catalog)
    except Exception as e:
        return JSONResponse({"models": [], "error": f"Couldn't reach NVIDIA: {type(e).__name__}"})
    return JSONResponse({"models": [{"id": m, "label": nim.label(m), "vision": nim.is_vision(m)} for m in ids]})


async def nim_test_model(request: Request) -> JSONResponse:
    """Settings' Test on one model: sees a screenshot? calls tools? how fast?"""
    model = str((await request.json()).get("model", "")).strip()
    if not model or not nim.nim_key():
        return JSONResponse({"ok": False, "note": "No model or no key."})
    r = await asyncio.to_thread(nim.test_model, model)
    return JSONResponse({"ok": r["tools"], **r})


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
            missing = [nim.label(m) for m in (state["settings"].get("brain_models") or nim.BRAIN_MODELS) if m not in listed]
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
    return JSONResponse({**plugins.public(), "workshop": await asyncio.to_thread(workshop.public)})


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


# ---------- remote: pairing a phone, the app shell, waking the PC ----------

async def pair_page(_request: Request) -> HTMLResponse:
    return HTMLResponse((HERE / "static" / "pair.html").read_text(encoding="utf-8"))


async def pair_device(request: Request) -> JSONResponse:
    body = await request.json()
    token = remote.pair(str(body.get("code", "")), str(body.get("name", "")))
    if not token:
        return JSONResponse({"error": "That code is wrong or has expired. Make a new one in IO's Settings > Remote on the PC."}, status_code=403)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(remote.COOKIE, token, max_age=365 * 86400, httponly=True, secure=True, samesite="strict", path="/")
    return resp


async def pair_code(_request: Request) -> JSONResponse:
    if not state["settings"].get("remote_access"):
        return JSONResponse({"error": "turn remote access on first"}, status_code=400)
    p = remote.new_code()
    return JSONResponse({"code": p["code"], "expires": p["expires"]})


async def api_token(request: Request) -> JSONResponse:
    """Settings > Remote: a token for another program (shown once). On the PC only, like making a pairing code."""
    if not state["settings"].get("remote_access"):
        return JSONResponse({"error": "turn remote access on first"}, status_code=400)
    name = str((await request.json()).get("name") or "API").strip() or "API"
    return JSONResponse({"token": remote.new_token(name), "devices": remote.devices()})


async def get_task(request: Request) -> JSONResponse:
    """One task's outcome, for programs: its status, answer, and any question it waits on (answer it with
    POST /api/tasks/{id}/answer)."""
    t = next((t for t in state["tasks"] if t["id"] == request.path_params["id"]), None)
    if t is None:
        return JSONResponse({"error": "no such task"}, status_code=404)
    return JSONResponse({k: t.get(k) for k in ("id", "status", "text", "summary", "question", "chat_id", "created", "finished")})


async def revoke_device(request: Request) -> JSONResponse:
    return JSONResponse({"ok": remote.revoke(request.path_params["id"]), "devices": remote.devices()})


# ---------- notifications on paired phones ----------

PUSH_HOSTS = (".push.apple.com", "fcm.googleapis.com", ".notify.windows.com", ".push.services.mozilla.com")


def phone_looking() -> bool:
    """The phone app is open on screen: it polls every 2.5 s while you look at it, and not at all in the background."""
    return time.time() - remote.last_remote[0] < 10


def push_phones(title: str, body: str, url: str = "/", tag: str = "io") -> None:
    targets = remote.push_targets()
    if not targets or phone_looking():
        return
    message = {"title": title[:80], "body": body[:240], "url": url, "tag": tag}

    def send() -> None:
        for device_id, sub in targets:
            if webpush.send(sub, message) in (404, 410):  # the phone turned them off or removed the app
                remote.set_push(device_id, None)

    threading.Thread(target=send, daemon=True).start()


def push_finished(task: dict) -> None:
    """What reaches the phone: answers to what you asked from it, goals reaching their end, and unattended runs that
    failed. Everything else you'll see when you look."""
    kind = task.get("source", "").split(":")[0]
    goal = next((g for g in state["goals"] if g["id"] == task.get("goal")), None) if task.get("goal") else None
    where = f"/#chat/{task['chat_id']}" if task.get("chat_id") else "/#history"
    summary = boss.clean_summary(task.get("summary") or "") or task["status"]
    if goal and goal.get("status") == "done":
        push_phones(f"Goal complete: {goal.get('title', '')}", summary, where, f"goal-{goal['id']}")
    elif task.get("from_phone") and task["status"] in ("done", "error"):
        push_phones(("Failed: " if task["status"] == "error" else "") + task["text"][:60], summary, where, task["id"])
    elif task["status"] == "error" and kind in ("schedule", "trigger", "webhook", "goal"):
        push_phones(f"{task['source'][:60]} failed", summary, where, task["id"])


def push_question(task: dict) -> None:
    if task.get("approval"):
        push_phones("Approval needed", task.get("question", "").removeprefix("Approval needed: "), "/#approvals", f"approval-{task['approval']}")
    else:
        push_phones("IO has a question", task.get("question", ""), f"/#chat/{task['chat_id']}" if task.get("chat_id") else "/#history", task["id"])


finished_listeners.append(push_finished)
question_listeners.append(push_question)


async def push_key(_request: Request) -> JSONResponse:
    return JSONResponse({"key": await asyncio.to_thread(webpush.public_key)})


async def push_subscribe(request: Request) -> JSONResponse:
    """The phone turns its notifications on (a subscription) or off (null)."""
    device = remote.device_for(remote.token_of(request.scope))
    if device is None:
        return JSONResponse({"error": "notifications are for a paired phone"}, status_code=400)
    sub = (await request.json()).get("subscription")
    if sub is not None:
        endpoint = str((sub or {}).get("endpoint", ""))
        host = urllib.parse.urlsplit(endpoint).hostname or ""
        keys = sub.get("keys") or {}
        if not endpoint.startswith("https://") or not any(host == h.lstrip(".") or host.endswith(h) for h in PUSH_HOSTS) \
                or not keys.get("p256dh") or not keys.get("auth"):
            return JSONResponse({"error": "that isn't a push service IO knows"}, status_code=400)
        sub = {"endpoint": endpoint, "keys": {"p256dh": str(keys["p256dh"]), "auth": str(keys["auth"])}}
    remote.set_push(device["id"], sub)
    return JSONResponse({"ok": True})


async def push_test(request: Request) -> JSONResponse:
    device = remote.device_for(remote.token_of(request.scope))
    sub = next((s for d, s in remote.push_targets() if device and d == device["id"]), None)
    if sub is None:
        return JSONResponse({"error": "notifications aren't on for this phone"}, status_code=400)
    code = await asyncio.to_thread(webpush.send, sub, {"title": "IO", "body": "Notifications work.", "url": "/", "tag": "test"})
    return JSONResponse({"ok": code in (200, 201), "status": code})


async def remote_status(_request: Request) -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(remote.tailscale_status, PORT))


async def remote_serve(_request: Request) -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(remote.tailscale_serve, PORT))


async def ping(_request: Request) -> JSONResponse:
    """Is IO up (the phone's wake screen polls this after asking Home Assistant to wake the PC)."""
    return JSONResponse({"ok": True, "machine": os.environ.get("COMPUTERNAME", ""), "busy": current["task"] is not None})


async def manifest(_request: Request) -> JSONResponse:
    return JSONResponse({
        "name": "IO", "short_name": "IO", "description": "Your PC's assistant", "start_url": "/", "scope": "/",
        "display": "standalone", "background_color": "#ffffff", "theme_color": "#ffffff",
        "icons": [{"src": "/static/io-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/static/io-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
    }, media_type="application/manifest+json")


async def service_worker(_request: Request) -> Response:
    # served from the root so it can look after the whole app; never cached by the browser itself
    return Response((HERE / "static" / "sw.js").read_text(encoding="utf-8"), media_type="application/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


async def offline_page(_request: Request) -> HTMLResponse:
    return HTMLResponse((HERE / "static" / "offline.html").read_text(encoding="utf-8"))


ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
# what a paired phone still can't do from afar: open IO wider or change what it may touch (keys, plugins, settings,
# devices, the browser bridge, tool checks). Everything about using IO (chats, runs, goals, approvals) works.
REMOTE_DENY = ("/api/remote/", "/api/keys/", "/api/plugins/", "/api/settings", "/api/browser", "/api/skills", "/api/toolcheck",
               "/api/learned/", "/api/nim/", "/api/open",  # /api/open: a tapped path would open on the PC's screen
               "/api/quit", "/api/show", "/api/customize", "/api/workshop/")  # quitting would leave the phone nothing to reach


class SameOriginOnly:
    """Web pages on other sites can't send commands here (queue a task, change settings, install plugins). A remote
    page is allowed only from its own origin (the tailnet address it was served from)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] not in ("GET", "HEAD", "OPTIONS") and not scope["path"].startswith("/api/hook/"):
            headers = dict(scope["headers"])
            origin = headers.get(b"origin")
            if origin is not None:
                o = origin.decode("latin-1")
                host = headers.get(b"host", b"").decode("latin-1")
                if o not in ALLOWED_ORIGINS and o.split("://", 1)[-1] != host:
                    return await PlainTextResponse("forbidden", status_code=403)(scope, receive, send)
        await self.app(scope, receive, send)


class RemoteGate:
    """127.0.0.1 and localhost: the desktop window, as always. Any other host is a request through Tailscale's proxy: it
    gets through only with remote access turned on, and only from a paired device (the pairing page and the app shell
    excepted). This replaces the old localhost-only host check, which it keeps when remote access is off."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            if remote.is_remote(scope):
                path = scope["path"]
                if not state["settings"].get("remote_access"):
                    return await PlainTextResponse("IO's remote access is off (Settings > Remote)", status_code=403)(scope, receive, send)
                if not path.startswith(remote.PUBLIC_PATHS) and remote.device_for(remote.token_of(scope)) is None:
                    if path.startswith("/api/"):
                        return await JSONResponse({"error": "pair this device first"}, status_code=401)(scope, receive, send)
                    return await RedirectResponse("/pair", status_code=303)(scope, receive, send)
                if path.startswith(REMOTE_DENY):
                    return await JSONResponse({"error": "that can only be changed on the PC"}, status_code=403)(scope, receive, send)
        await self.app(scope, receive, send)


app = Starlette(
    middleware=[Middleware(GZipMiddleware, minimum_size=2000), Middleware(RemoteGate), Middleware(SameOriginOnly)],
    routes=[
        Route("/", index),
        Route("/pair", pair_page),
        Route("/api/pair", pair_device, methods=["POST"]),
        Route("/api/remote/code", pair_code, methods=["POST"]),
        Route("/api/remote/devices/{id}/revoke", revoke_device, methods=["POST"]),
        Route("/api/remote/status", remote_status),
        Route("/api/remote/token", api_token, methods=["POST"]),
        Route("/api/tasks/{id}", get_task, methods=["GET"]),
        Route("/api/push/key", push_key),
        Route("/api/push/subscribe", push_subscribe, methods=["POST"]),
        Route("/api/push/test", push_test, methods=["POST"]),
        Route("/api/remote/serve", remote_serve, methods=["POST"]),
        Route("/api/ping", ping),
        Route("/manifest.webmanifest", manifest),
        Route("/sw.js", service_worker),
        Route("/offline.html", offline_page),
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
        Route("/api/nim/models", nim_catalog),
        Route("/api/nim/test", nim_test_model, methods=["POST"]),
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
        Route("/api/open", open_target, methods=["POST"]),
        Route("/api/goals", save_goal, methods=["POST"]),
        Route("/api/goals/{id}/{action}", goal_action, methods=["POST"]),
        Route("/api/approvals/{id}", decide_approval, methods=["POST"]),
        Route("/api/workshop/{id}", workshop_action, methods=["POST"]),
        Route("/api/paths", existing_paths, methods=["POST"]),
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
