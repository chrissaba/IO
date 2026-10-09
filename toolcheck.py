"""Smoke-tests every tool the boss can use without changing anything on screen, and the two local model servers:
Muse Glimmer (llama-server on :8090, thinks) and EvoCUA (:8091, sees and clicks)."""
import asyncio, contextlib, json, sys, time, urllib.error, urllib.request
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import boss

def row(name, ok, detail, t0):
    print(f"{'OK ' if ok else 'FAIL'} {name:<16} {time.time()-t0:5.1f}s  {detail[:90]}")

def skip(name, detail):
    print(f"--   {name:<16}         {detail[:90]}")

def get_json(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read() or b"{}")

def model_server(name, base):
    """A llama-server is ready when /health says ok (it answers 503 while the model loads); /props names the model file."""
    t0, root = time.time(), base.removesuffix("/v1")
    try:
        ok = get_json(root + "/health").get("status") == "ok"
        detail = "ready" if ok else "not ready"
        with contextlib.suppress(Exception):
            detail = Path(str(get_json(root + "/props").get("model_path", ""))).name or detail
    except urllib.error.HTTPError as e:
        ok, detail = False, f"HTTP {e.code} (still loading?)"
    except Exception as e:
        ok, detail = False, f"not reachable: {e}"
    row(name, ok, f"{root}  {detail}", t0)
    return ok

def evo_eyes():
    """EvoCUA's eyes. boss.Eyes() has one mode now; an older boss.py defaulted to UI-TARS and took "balanced" for EvoCUA."""
    eyes = boss.Eyes()
    if getattr(eyes, "mode", "evo") != "evo":
        eyes = boss.Eyes("balanced")
    return eyes

async def main():
    model_server("Glimmer :8090", boss.BOSS_URL)
    evo_ok = model_server("EvoCUA :8091", getattr(boss, "EVO_URL", "http://127.0.0.1:8091/v1"))
    server = StdioServerParameters(command=str(Path(sys.executable).with_name("python.exe")), args=["-m", "windows_mcp", "serve", "--tools", boss.MCP_TOOLS])
    async with stdio_client(server, errlog=open("logs/toolcheck-mcp.log", "w")) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        names = [t.name for t in (await s.list_tools()).tools]
        print("Windows-MCP tools loaded:", ", ".join(names))
        checks = [
            ("Snapshot", {"use_vision": False, "use_annotation": False}, lambda t: "Visible Displays" in t),
            ("Wait", {"duration": 1}, lambda t: "Waited" in t),
            ("WaitFor", {"condition": "text_exists", "text": "IO", "timeout": 3}, lambda t: "satisf" in t.lower() or "matched" in t.lower()),
            ("Process", {"mode": "list", "limit": 3}, lambda t: len(t) > 20),
            ("PowerShell", {"command": "Write-Output toolcheck-ok"}, lambda t: "toolcheck-ok" in t),
            ("Clipboard", {"mode": "get"}, lambda t: not t.lower().startswith("error")),
        ]
        for name, args, good in checks:
            t0 = time.time()
            try:
                res = await s.call_tool(name, args)
                text = "\n".join(getattr(p, "text", "") for p in res.content if getattr(p, "type", "") == "text")
                row(name, good(text), "content hidden" if name == "Clipboard" else text.replace("\n", " "), t0)
            except Exception as e:
                row(name, False, str(e), t0)
    if not evo_ok:
        for name in ("find_on_screen", "look_at_screen"):
            skip(name, "skipped: EvoCUA on :8091 isn't ready")
        return
    eyes = evo_eyes()
    t0 = time.time(); r = eyes.find("the Start button on the taskbar"); row("find_on_screen", "x" in r, json.dumps(r), t0)
    t0 = time.time(); d = eyes.describe("In one sentence, what app is in the foreground?"); row("look_at_screen", len(d) > 5, d.replace("\n", " "), t0)

asyncio.run(main())
