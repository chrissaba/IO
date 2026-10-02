"""Smoke-tests every tool the boss can use without changing anything on screen."""
import asyncio, json, sys, time
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import boss

def row(name, ok, detail, t0):
    print(f"{'OK ' if ok else 'FAIL'} {name:<16} {time.time()-t0:5.1f}s  {detail[:90]}")

async def main():
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
    eyes = boss.Eyes()
    t0 = time.time(); r = eyes.find("the Start button on the taskbar"); row("find_on_screen", "x" in r, json.dumps(r), t0)
    t0 = time.time(); d = eyes.describe("In one sentence, what app is in the foreground?"); row("look_at_screen", len(d) > 5, d.replace("\n", " "), t0)

asyncio.run(main())
