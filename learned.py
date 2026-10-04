"""Skills IO teaches itself: after a task (and every so often in a long loop) the brain rewrites a short playbook for that
kind of task from what just happened (what worked, where things are, what to avoid), and the playbook is handed back
to it the next time a similar task comes up. Kept in data/learned_skills.json; Settings lists them and can delete them."""
import json
import re
import threading
import time
from pathlib import Path

HERE = Path(__file__).parent
FILE = HERE / "data" / "learned_skills.json"
MAX_SKILL_CHARS = 3000  # one playbook
RECALL_BUDGET = 6000  # all playbooks handed to one task
MIN_STEPS = 4  # a task shorter than this teaches nothing worth keeping
_lock = threading.Lock()
STOP = {"the", "and", "for", "with", "that", "this", "you", "your", "from", "into", "until", "tell", "stop", "want", "try",
        "can", "please", "then", "open", "have", "has", "just", "keep", "going", "play", "game", "app", "window", "it", "its",
        # words nearly every PC task shares: an expense-tracker build matched a "serve a static page" playbook on folder,
        # documents, browser, server, http, proof and png alone, and followed its steps (wrong server, wrong folder)
        "folder", "folders", "documents", "desktop", "downloads", "file", "files", "browser", "page", "web", "site", "new",
        "make", "build", "create", "write", "save", "screenshot", "localhost", "http", "https", "www", "com", "org", "net",
        "server", "run", "start", "use", "show", "called", "png", "jpg", "txt", "html", "proof", "test", "tests", "finish"}

LEARN_SYSTEM = """You maintain a playbook that an AI agent on a Windows PC reads before doing this kind of task again.
You get the current playbook (may be empty) and a log of the latest run: the request, each action with its result,
and how it ended. Rewrite the playbook so the next run is faster and makes fewer mistakes:
- concrete facts: where buttons and menus are, what each screen means, the order that works, good settings or targets
- what failed or wasted time, and what to do instead
- for games: the progression loop, what to buy or upgrade first, how to spot and close pop-ups and ads
Keep what is still true from the old playbook, fix what the run proved wrong, drop guesses. Write down only facts the run
verified. Keep what was specific to this request (its file names, ports, the kind of page) apart from what holds for any
run, so a similar but different request doesn't copy it. At most 2500 characters, short lines, no story of the run.
Reply ONLY with JSON: {"name": "<short name of the task or app, e.g. Idle Obelisk Miner>", "keywords": ["3-8 words specific
to this kind of task (app, game, site or product names), never general ones like folder, browser, server, file or page"],
"playbook": "..."}"""


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower()) if w not in STOP}


def load() -> list[dict]:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(skills: list[dict]) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(skills, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(FILE)


def _score(skill: dict, words: set) -> int:
    return len(words & (_words(skill.get("name", "")) | {w for k in skill.get("keywords", []) for w in _words(k)}))


def match(text: str) -> dict | None:
    """The playbook that best fits a request or window title (at least two shared words, or its whole name)."""
    words = _words(text)
    best = max(load(), key=lambda s: _score(s, words), default=None)
    if best and (_score(best, words) >= 2 or _words(best.get("name", "")) <= words and _words(best.get("name", ""))):
        return best
    return None


def recall(text: str) -> str:
    """The text handed to a task: the playbooks that fit it, best first, within RECALL_BUDGET."""
    words = _words(text)
    picked, used = [], 0
    for s in sorted(load(), key=lambda s: -_score(s, words)):
        name_words = _words(s.get("name", ""))
        if not (_score(s, words) >= 2 or (name_words and name_words <= words)):
            continue
        block = f"## {s['name']} (learned from {s.get('runs', 1)} earlier run{'s' if s.get('runs', 1) != 1 else ''})\n{s['playbook']}"
        if picked and used + len(block) > RECALL_BUDGET:
            break
        picked.append(block)
        used += len(block)
        with _lock:  # count the use
            skills = load()
            for k in skills:
                if k["name"] == s["name"]:
                    k["uses"] = k.get("uses", 0) + 1
            _save(skills)
    if not picked:
        return ""
    # notes, not orders: "follow them" made a run copy a static-page recipe into an app build that needed its own server
    return ("Notes you wrote yourself on earlier runs of similar-looking tasks. Use what fits; the request always comes first: "
            "where it asks for something different (another app, server, folder, file or order of steps), do what it asks, "
            "not what these notes did. Facts in them can be out of date.\n" + "\n\n".join(picked))


def learn(ask, request: str, steps: list[str], outcome: str, window: str = "") -> str:
    """Updates (or starts) the playbook for this kind of task from one run. `ask(system, user) -> reply text` is the
    brain. Returns the playbook's name, or '' when nothing was learned."""
    if len(steps) < MIN_STEPS:
        return ""
    old = match(f"{request} {window}")
    log_text = "\n".join(s[:400] for s in steps[-80:])[-24000:]
    user = (f"Current playbook ({old['name']}):\n{old['playbook']}\n\n" if old else "Current playbook: (none yet)\n\n") + \
           f"Request: {request}\n" + (f"Window: {window}\n" if window else "") + f"\nLog of the latest run:\n{log_text}\n\nHow it ended: {outcome[:1500]}"
    reply = ask(LEARN_SYSTEM, user) or ""
    m = re.search(r"\{.*\}", re.sub(r"<think>.*?</think>", "", reply, flags=re.S), re.S)
    try:
        data = json.loads(m.group(0)) if m else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or not str(data.get("playbook") or "").strip():
        return ""
    name = (old["name"] if old else str(data.get("name") or request[:40])).strip()[:60]
    keywords = [str(k)[:40] for k in (data.get("keywords") or []) if str(k).strip()][:10]
    with _lock:
        skills = [s for s in load() if s["name"] != name]
        skills.append({"name": name, "keywords": sorted(set(keywords) | set(old.get("keywords", []) if old else []))[:12],
                       "playbook": str(data["playbook"]).strip()[:MAX_SKILL_CHARS], "runs": (old.get("runs", 1) + 1) if old else 1,
                       "uses": old.get("uses", 0) if old else 0, "updated": time.time()})
        _save(skills)
    return name


def delete(name: str) -> bool:
    with _lock:
        skills = load()
        kept = [s for s in skills if s["name"] != name]
        if len(kept) == len(skills):
            return False
        _save(kept)
        return True
