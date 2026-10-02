"""Cloud planners for the boss agent.

A free-tier cloud model writes the plan (once per task, plus a few replans when the local boss
gets stuck); the local boss still does every action. Providers are tried in order, a provider
that rate-limits us is skipped for a while, and with none available the boss plans by itself.

Providers live in planners.json (edited from the control panel), e.g.
    [{"name": "Groq", "base_url": "https://api.groq.com/openai/v1",
      "model": "openai/gpt-oss-120b", "api_key": "..."}]
"""
import json
import re
import time
from pathlib import Path

from openai import APIStatusError, OpenAI

CONFIG = Path(__file__).parent / "planners.json"
USAGE = Path(__file__).parent / "data" / "planner_usage.json"
CACHE = Path(__file__).parent / "data" / "plan_cache.json"
CACHE_DAYS = 7

# presets shown in the control panel; any OpenAI-compatible endpoint works
PRESETS = [
    {"name": "Groq", "base_url": "https://api.groq.com/openai/v1", "model": "openai/gpt-oss-120b", "daily_limit": 1000},
    {"name": "Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "model": "gemini-3.1-flash-lite:500, gemini-3.5-flash-lite, gemini-3.5-flash, gemini-3.8-flash, gemini-3-flash-preview:20", "daily_limit": 0},
    {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "model": ""},
    {"name": "Cerebras", "base_url": "https://api.cerebras.ai/v1", "model": ""},
]

SYSTEM = """You plan tasks for a Windows desktop agent. A small local model carries out your plan with these tools:
App (launch or switch to an app by name), Snapshot (text list of open windows and on-screen controls with coordinates),
Click/Type (at coordinates from Snapshot), type_text (type into the focused control), Shortcut (keyboard shortcuts),
Scroll, Wait/WaitFor, PowerShell, Clipboard, Process, find_on_screen (visually locate something not in Snapshot),
look_at_screen (answer a question about what a display shows), browser_open and browser_* tools (open a page in IO's own browser tab, then click and type on it by element),
FileSystem (read/write files), Scrape (read a web page as text), ask_user (ask the user a question), remember (save a note),
done (finish, with the answer in its summary).

Write a short numbered plan, at most 8 steps. Each step is one concrete action naming the tool to use.
Prefer App launches, keyboard shortcuts and PowerShell over clicking. Do only what the task asks.
For websites, or when the user mentions the browser, Chrome or IO's tab, start with browser_open and use browser_* tools; never App, Click, Type or Shortcut on a browser window.
PowerShell is a tool that runs a command and returns its output: never open a PowerShell or Terminal window to run one.
To read text in a window, use Snapshot (it lists the text of controls) or look_at_screen, never select-all and copy: that replaces the user's clipboard.
For games and emulators, use find_on_screen/look_at_screen with window set to the app's title, and close menus with their X button, never Esc/Back.
Plans are reused for repeated tasks: write steps, never answers or facts read from the current state.
If the message is conversation rather than something to do on the PC (a greeting, thanks, small talk, or a question
answerable from general knowledge), output exactly NO_PLAN.
Output only the plan."""

# provider name -> time.time() until which it is skipped after a rate limit or outage
cooldown: dict[str, float] = {}


def load_all() -> list[dict]:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def load() -> list[dict]:
    """Providers that are usable: they have both a key and a model."""
    return [p for p in load_all() if p.get("api_key") and p.get("model")]


def save(providers: list[dict]) -> None:
    CONFIG.write_text(json.dumps(providers, indent=2), encoding="utf-8")


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, data) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def usage() -> dict[str, int]:
    """Requests sent to each provider today (local date)."""
    u = _read(USAGE, {})
    return u.get("counts", {}) if u.get("date") == time.strftime("%Y-%m-%d") else {}


def _count(name: str) -> None:
    counts = usage()
    counts[name] = counts.get(name, 0) + 1
    _write(USAGE, {"date": time.strftime("%Y-%m-%d"), "counts": counts})


def _cache_key(task: str) -> str:
    return " ".join(task.lower().split())


def _cached(task: str) -> dict | None:
    hit = _read(CACHE, {}).get(_cache_key(task))
    return hit if hit and time.time() - hit["ts"] < CACHE_DAYS * 86400 else None


def _store(task: str, text: str, source: str) -> None:
    cache = {k: v for k, v in _read(CACHE, {}).items() if time.time() - v["ts"] < CACHE_DAYS * 86400}
    cache[_cache_key(task)] = {"plan": text, "source": source, "ts": time.time()}
    _write(CACHE, cache)


def models_of(provider: dict) -> list[tuple[str, int]]:
    """A provider's model field may list several models to rotate through, each optionally with its
    own daily limit: "gemini-3.1-flash-lite:500, gemini-3-flash-preview:20". No limit given means the
    provider's daily_limit (0 = none)."""
    default = int(provider.get("daily_limit") or 0)
    out = []
    for item in str(provider.get("model", "")).split(","):
        item = item.strip()
        # only a trailing ":<digits>" is a limit; OpenRouter ids like "deepseek/deepseek-r1:free" keep their suffix
        name, sep, limit = item.rpartition(":")
        if not (sep and limit.strip().isdigit()):
            name, limit = item, ""
        name = name.strip()
        if name:
            out.append((name, int(limit) if limit.strip().isdigit() else default))
    return out


def candidates(providers: list[dict]) -> list[dict]:
    """Every (provider, model) pair in the order they should be tried."""
    return [
        {**p, "model": model, "daily_limit": limit, "slot": f"{p['name']}/{model}" if len(models_of(p)) > 1 else p["name"]}
        for p in providers
        for model, limit in models_of(p)
    ]


def _until_midnight_pacific() -> float:
    """Google resets free-tier daily quotas at midnight Pacific time. This PC runs on Pacific time,
    so local midnight is used (Windows Python ships without the tz database)."""
    from datetime import datetime, timedelta

    now = datetime.now()
    return (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1) - now).total_seconds()


def _rate_limit_wait(e: APIStatusError) -> tuple[float, bool]:
    """(seconds to skip this model, whether it was the daily quota) from a 429."""
    text = str(e)
    if "PerDay" in text or "per day" in text.lower():
        try:
            return _until_midnight_pacific(), True
        except Exception:
            return 3600.0, True
    m = re.search(r"retry in ([\d.]+)s", text)
    if m:
        return float(m.group(1)) + 1, False
    try:
        return float(e.response.headers.get("retry-after", 60)), False
    except (TypeError, ValueError):
        return 60.0, False


def ask(provider: dict, messages: list[dict], timeout: float = 60) -> str:
    client = OpenAI(base_url=provider["base_url"], api_key=provider["api_key"], max_retries=0, timeout=timeout)
    reply = client.chat.completions.create(model=provider["model"], messages=messages, temperature=0.2, max_tokens=800)
    text = reply.choices[0].message.content or ""
    # some reasoning models inline their thinking
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def plan(task: str, history: str = "", context: str = "", cacheable: bool = False, cache_salt: str = "") -> tuple[str, str]:
    """Returns (plan, provider name), or ("", reason) when no cloud planner could answer.
    context: local facts gathered on the PC (saved notes, open windows), when the user allows sharing them.
    cacheable: reuse/keep this task's first plan for a week (for scheduled and triggered tasks, which repeat);
    replans (history given) always ask fresh. cache_salt: changes when the agent's plugins or skills change,
    so a cached plan written without them isn't reused."""
    providers = load()
    if not providers:
        return "", "no cloud planner configured"
    cache_task = task + ("|" + cache_salt if cache_salt else "")
    if cacheable and not history and (hit := _cached(cache_task)):
        return hit["plan"], f"{hit['source']}, cached"
    user = f"Task: {task}"
    if context:
        user += f"\n\nCurrent state of the PC:\n{context}"
    if history:
        user += f"\n\nThe agent got stuck. Recent actions and results:\n{history}\n\nWrite a new plan from the current state."
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    reasons = []
    # rotate through every model of every provider; a rate-limited one sits out until its limit resets
    for c in candidates(providers):
        slot = c["slot"]
        if cooldown.get(slot, 0) > time.time():
            reasons.append(f"{slot} resting")
            continue
        if c["daily_limit"] and usage().get(slot, 0) >= c["daily_limit"]:
            reasons.append(f"{slot} used its {c['daily_limit']} today")
            continue
        try:
            _count(slot)
            text = ask(c, messages)
            if text:
                if cacheable and not history:
                    _store(cache_task, text, slot)
                return text, slot
            reasons.append(f"{slot} returned nothing")
        except APIStatusError as e:
            if e.status_code == 429:
                wait, daily = _rate_limit_wait(e)
                cooldown[slot] = time.time() + wait
                reasons.append(f"{slot} {'out for today' if daily else 'rate-limited'}")
            else:
                cooldown[slot] = time.time() + 300
                reasons.append(f"{slot} error {e.status_code}")
        except Exception as e:  # network trouble: skip this one for a bit
            cooldown[slot] = time.time() + 120
            reasons.append(f"{slot} unreachable ({type(e).__name__})")
    return "", "; ".join(reasons)


def test(provider: dict) -> str:
    """One tiny request per listed model to check the key and model names."""
    results = []
    for c in candidates([provider]):
        try:
            ask(c, [{"role": "user", "content": "Reply with: ok"}], timeout=30)
            results.append(f"{c['model']}: ok")
        except APIStatusError as e:
            results.append(f"{c['model']}: {'rate-limited right now' if e.status_code == 429 else f'error {e.status_code}'}")
        except Exception as e:
            results.append(f"{c['model']}: {type(e).__name__}")
    return " · ".join(results)
