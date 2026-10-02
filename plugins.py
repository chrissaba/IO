"""Plugins (MCP servers) and skills for IO, managed from the Customize page.

- catalog.json lists plugins that can be installed with one click (command + args, any keys they need).
- data/plugins.json records what's installed, its settings, and your skills.
- Installed, enabled plugins are started next to Windows-MCP for every task; their tools are given to the
  boss as "<plugin>_<tool>". Skills that fit the task are added next to it.
"""
import json
import os
import re
import shutil
import sys
import time
import uuid
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).parent
CATALOG = HERE / "catalog.json"
STATE = HERE / "data" / "plugins.json"
START_TIMEOUT = 90  # the first start of an npx/uvx plugin downloads it
RETRY_AFTER = 600  # a plugin that failed to start is skipped this long, instead of costing START_TIMEOUT every task
TOOL_BUDGET = 5000  # rough tokens of plugin tool definitions the boss's 32K context can spare
MAX_SKILL = 4000
SKILLS_BUDGET = 3000  # characters of skills added to one task
# environment a plugin gets on top of mcp's own minimal set: no tokens or keys from your environment
ENV_KEEP = ("COMSPEC", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "WINDIR", "TMP", "HOME", "NUMBER_OF_PROCESSORS", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
            "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE")
_failed: dict[str, float] = {}  # plugin id -> when it last failed to start


def catalog() -> list[dict]:
    try:
        return json.loads(CATALOG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def load() -> dict:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
    except OSError:
        data = {}
    except ValueError:
        # keep the unreadable file instead of overwriting it with an empty state on the next save
        bad = STATE.with_name(f"plugins.json.bad-{int(time.time())}")
        STATE.replace(bad)
        print("plugins.json was unreadable; kept a copy as", bad)
        data = {}
    data.setdefault("installed", {})
    data.setdefault("skills", [])
    return data


def save(data: dict) -> None:
    STATE.parent.mkdir(exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def missing_settings(entry: dict, settings: dict) -> list[dict]:
    return [e for e in entry.get("env", []) if not settings.get("env", {}).get(e["name"])]


def public() -> dict:
    """Catalog + installed state for the UI. Secret values are hidden; plain settings (folders, files) are shown."""
    data = load()
    entries = {c["id"]: c for c in catalog()}
    installed = {}
    for pid, p in data["installed"].items():
        entry = entries.get(pid, {})
        installed[pid] = {
            **p,
            "env": {e["name"]: p.get("env", {}).get(e["name"], "") for e in entry.get("env", []) if not e.get("secret")},
            "env_set": [k for k, v in p.get("env", {}).items() if v],
            "missing": [e["label"] for e in missing_settings(entry, p)],
        }
    return {"catalog": catalog(), "installed": installed, "skills": data["skills"]}


def install(pid: str, enabled: bool = True, env: dict | None = None) -> dict:
    entry = next((c for c in catalog() if c["id"] == pid), None)
    if entry is None:
        raise KeyError(pid)
    allowed = {e["name"] for e in entry.get("env", [])}  # only the settings this plugin declares
    data = load()
    current = data["installed"].get(pid, {"installed_at": time.time(), "env": {}})
    for k, v in (env or {}).items():
        if v and k in allowed:  # blank keeps the saved value
            current["env"][k] = str(v)
    current["enabled"] = bool(enabled)
    data["installed"][pid] = current
    save(data)
    _failed.pop(pid, None)
    return current


def uninstall(pid: str) -> None:
    data = load()
    data["installed"].pop(pid, None)
    save(data)


def save_skill(skill: dict) -> dict:
    data = load()
    s = {
        "id": skill.get("id") or uuid.uuid4().hex[:8],
        "name": str(skill.get("name", "")).strip()[:60],
        "instructions": str(skill.get("instructions", "")).strip(),
        "enabled": bool(skill.get("enabled", True)),
    }
    if not s["name"] or not s["instructions"]:
        raise ValueError("a skill needs a name and instructions")
    if len(s["instructions"]) > MAX_SKILL:
        raise ValueError(f"instructions are limited to {MAX_SKILL} characters")
    data["skills"] = [x for x in data["skills"] if x["id"] != s["id"]] + [s]
    save(data)
    return s


def delete_skill(sid: str) -> None:
    data = load()
    data["skills"] = [x for x in data["skills"] if x["id"] != sid]
    save(data)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def skills_prompt(task: str = "") -> str:
    """The enabled skills that best fit the task (by shared words with each skill's name and first line),
    up to SKILLS_BUDGET characters. With only one or two skills, they all go in."""
    enabled = [s for s in load()["skills"] if s.get("enabled", True)]
    if not enabled:
        return ""
    words = _words(task)
    scored = sorted(enabled, key=lambda s: -len(words & _words(s["name"] + " " + s["instructions"].split("\n")[0])))
    if len(enabled) > 2:
        scored = [s for s in scored if words & _words(s["name"] + " " + s["instructions"].split("\n")[0])] or scored[:1]
    picked, used = [], 0
    for s in scored:
        line = f"- {s['name']}: {s['instructions']}"
        if picked and used + len(line) > SKILLS_BUDGET:
            break
        picked.append(line)
        used += len(line)
    return "Skills that may apply (follow the matching one):\n" + "\n".join(picked)


def _params(entry: dict, settings: dict) -> StdioServerParameters:
    values = {k: v for k, v in settings.get("env", {}).items() if v}
    command = entry["command"]
    # <NAME> in args is filled from the plugin's settings (e.g. a vault folder or database file)
    args = [re.sub(r"<([A-Z_]+)>", lambda m: values.get(m.group(1), m.group(0)), a) for a in entry.get("args", [])]
    if command == "npx" and sys.platform == "win32":
        # npx is a batch file, and cmd.exe would read & | < > ^ % in a folder name as commands: run npm's own script with node
        node = shutil.which("node")
        cli = Path(node).parent / "node_modules" / "npm" / "bin" / "npx-cli.js" if node else None
        if cli and cli.exists():
            command, args = node, [str(cli), *args]
        else:
            command = "npx.cmd"
            if any(re.search(r'["&|<>^%]', a) for a in args):
                raise ValueError("a setting contains characters cmd.exe can't pass safely")
    env = {k: os.environ[k] for k in ENV_KEEP if k in os.environ}
    env.update({k: v for k, v in os.environ.items() if k.startswith(("UV_", "NPM_CONFIG_", "npm_config_"))})
    env.update({"PYTHONIOENCODING": "utf-8", **values})
    if "GITHUB_PERSONAL_ACCESS_TOKEN" in values:
        env["GITHUB_AUTH_HEADER"] = "Bearer " + values["GITHUB_PERSONAL_ACCESS_TOKEN"]
    return StdioServerParameters(command=command, args=args, env=env)


def _tool_name(pid: str, name: str) -> str:
    prefix = re.sub(r"[^a-zA-Z0-9]", "", pid)[:16]
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"{prefix}_{name}")[:64]


async def _start(stack: AsyncExitStack, entry: dict, settings: dict) -> tuple[ClientSession, list]:
    """Starts one plugin on its own stack (closed again if it fails) and lists its tools."""
    ps = await stack.enter_async_context(AsyncExitStack())
    try:
        r, w = await ps.enter_async_context(stdio_client(_params(entry, settings), errlog=sys.stderr))
        session = await ps.enter_async_context(ClientSession(r, w, read_timeout_seconds=START_TIMEOUT))
        await session.initialize()
        tools = list((await session.list_tools()).tools)
    except Exception:
        await ps.aclose()
        raise
    allow = entry.get("tools")  # optional allowlist in the catalog, for servers with many tools
    return session, [t for t in tools if not allow or t.name in allow]


async def start_enabled(stack: AsyncExitStack, log=print) -> list[tuple[str, ClientSession, object]]:
    """Starts every enabled plugin; returns (exposed tool name, session, mcp tool) triples.
    A plugin that fails to start is skipped with a warning, never failing the task."""
    data = load()
    entries = {c["id"]: c for c in catalog()}
    out = []
    for pid, settings in data["installed"].items():
        if not settings.get("enabled", True) or pid not in entries:
            continue
        entry = entries[pid]
        missing = missing_settings(entry, settings)
        if missing:
            log(f"plugin {entry['name']} skipped: needs {', '.join(e['label'] for e in missing)}")
            continue
        if time.time() - _failed.get(pid, 0) < RETRY_AFTER:
            log(f"plugin {entry['name']} skipped: it failed to start a few minutes ago")
            continue
        try:
            session, tools = await _start(stack, entry, settings)
            out += [(_tool_name(pid, t.name), session, t) for t in tools]
        except Exception as e:
            _failed[pid] = time.time()
            log(f"plugin {entry['name']} unavailable: {type(e).__name__}: {e}")
    return out


def approx_tokens(defs) -> int:
    return int(len(json.dumps(defs, ensure_ascii=False)) / 3.5)


def fit_budget(plugin_tools: list, defs: dict, task: str, log=print) -> list:
    """Drops whole plugins until their tool definitions fit TOOL_BUDGET: plugins the task mentions go
    first, then the smallest. Each one left out is logged."""
    by_plugin: dict[str, list] = {}
    for item in plugin_tools:
        by_plugin.setdefault(item[0].split("_", 1)[0], []).append(item)
    sizes = {p: approx_tokens([defs[a] for a, _, _ in items]) for p, items in by_plugin.items()}
    if sum(sizes.values()) <= TOOL_BUDGET:
        return plugin_tools
    entries = {re.sub(r"[^a-zA-Z0-9]", "", c["id"])[:16]: c for c in catalog()}
    words = _words(task)

    def mentioned(p: str) -> bool:
        c = entries.get(p, {})
        return bool(words & _words(f"{c.get('id', p)} {c.get('name', '')} {c.get('category', '')}"))

    keep, used = [], 0
    for p in sorted(by_plugin, key=lambda p: (not mentioned(p), sizes[p])):
        if used + sizes[p] <= TOOL_BUDGET:
            keep.append(p)
            used += sizes[p]
        else:
            log(f"plugin {entries.get(p, {}).get('name', p)} left out: its tools would overflow the model's context")
    return [item for p in keep for item in by_plugin[p]]


async def test(pid: str) -> str:
    """Starts one plugin, lists its tools, and stops it."""
    entry = next((c for c in catalog() if c["id"] == pid), None)
    settings = load()["installed"].get(pid)
    if entry is None or settings is None:
        return "install it first"
    missing = missing_settings(entry, settings)
    if missing:
        return "needs: " + ", ".join(e["label"] for e in missing)
    async with AsyncExitStack() as stack:
        try:
            _session, tools = await _start(stack, entry, settings)
        except Exception as e:
            _failed[pid] = time.time()
            return f"couldn't start: {type(e).__name__}: {e}"[:300]
    _failed.pop(pid, None)
    names = [t.name for t in tools]
    size = approx_tokens([{"name": t.name, "description": t.description, "schema": t.input_schema} for t in tools])
    return f"ok: {len(names)} tools, about {size} tokens ({', '.join(names[:8])}{'…' if len(names) > 8 else ''})"
