"""IO Console: chat with IO, call its tools directly, and watch everything it does as it happens: each model call's
thinking as it's written, its answer and tool calls, every API request (which model, why, how long it waited and
took, tokens, reasoning level), and what each tool returned. The chat in IO's window stays clean.

Opened by the Console button in IO's window, or: .venv\\Scripts\\python.exe console.py
It talks to IO at http://127.0.0.1:8765 (the live feed is ws://127.0.0.1:8765/api/live) and keeps no state of its own.
"""
import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.request

import websockets
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Input, RichLog, Static

BASE = f"http://127.0.0.1:{os.environ.get('BOSS_APP_PORT', '8765')}"
LIVE = BASE.replace("http://", "ws://") + "/api/live"
EFFORTS = ("low", "medium", "high", "max")
LIVE_LINES = 7  # lines of each stream's newest thinking shown while it's written
HELP = """Type a message to send it to IO (it runs like a message in IO's window, in this console's own chat).
/tool <name> key=value ...   call one of IO's tools directly, e.g. /tool read_file path="C:\\notes.txt" lines=1-40
/tools [word]                IO's tools (all, or those about a word)
/effort low|medium|high|max  the effort for your next messages
/new                         start a new chat     /chat <id>  continue one of IO's chats
/stop                        stop the task that's running
/quiet                       stop (or start again) writing whole thoughts into the log; they still show while written
/clear                       clear the log         /quit (or Ctrl+Q) close the console"""


def http(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(BASE + path, method=method, headers={"Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def short(value, n: int = 160) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_args(text: str) -> dict:
    """key=value pairs; quoted values keep their spaces and backslashes; numbers, true/false and JSON lists are parsed."""
    out = {}
    for m in re.finditer(r"(\w+)=(\"([^\"]*)\"|'([^']*)'|\S+)", text):
        raw = m.group(3) if m.group(3) is not None else m.group(4) if m.group(4) is not None else m.group(2)
        if m.group(3) is None and m.group(4) is None:
            try:
                raw = json.loads(raw)
            except ValueError:
                pass
        out[m.group(1)] = raw
    return out


class IOConsole(App):
    TITLE = "IO Console"
    CSS = """
    #status { height: 1; background: $boost; color: $text-muted; padding: 0 1; }
    #log { height: 1fr; border: none; padding: 0 1; }
    #live { height: auto; max-height: 45%; padding: 0 1; border-top: solid $primary 40%; }
    #live.idle { display: none; }
    #input { dock: bottom; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.effort = "high"
        self.chat_id = ""
        self.task_id = ""  # the task running now (anyone's)
        self.connected = False
        self.quiet = False
        self.streams: dict[str, dict] = {}  # rid -> the model call being written now
        self.dirty = False

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        yield RichLog(id="log", wrap=True, markup=False, highlight=False, max_lines=20000)
        yield Static(id="live", classes="idle")
        yield Input(placeholder="Message IO, or /help", id="input")

    def on_mount(self) -> None:
        self.log_line(Text("IO Console. Everything IO does shows here as it happens; /help for commands.", style="bold"))
        self.run_worker(self.listen(), exclusive=True)
        self.set_interval(0.1, self.repaint_live)
        self.set_interval(1.0, self.repaint_status)
        self.query_one("#input", Input).focus()

    # ------------------------------------------------------------------------------------------------ output

    def log_line(self, text) -> None:
        self.query_one("#log", RichLog).write(text if isinstance(text, Text) else Text(str(text)))

    def repaint_status(self) -> None:
        state = "connected" if self.connected else "not connected (is IO running?)"
        chat = self.chat_id or "new chat"
        busy = f" · working on task {self.task_id}" if self.task_id else ""
        self.query_one("#status", Static).update(f"IO Console · effort {self.effort} · {chat} · {state}{busy}")

    def repaint_live(self) -> None:
        if not self.dirty:
            return
        self.dirty = False
        live = self.query_one("#live", Static)
        if not self.streams:
            live.set_class(True, "idle")
            return
        out = Text()
        for s in self.streams.values():
            secs = time.time() - s["t0"]
            out.append(f"◆ {s['label']}  {secs:.0f}s\n", style="bold cyan")
            if s["think"]:
                lines = s["think"].rstrip().splitlines()[-LIVE_LINES:]
                out.append("\n".join("  " + ln for ln in lines) + "\n", style="italic grey58")
            if s["text"]:
                out.append("  " + s["text"].strip()[-600:] + "\n")
            if s["tool"]:
                out.append(f"  ▸ {s['tool']}({s['args'][-300:]}\n", style="yellow")
        live.update(out)
        live.set_class(False, "idle")

    # ------------------------------------------------------------------------------------------------ the feed

    async def listen(self) -> None:
        while True:
            try:
                async with websockets.connect(LIVE, max_size=None, ping_interval=20) as ws:
                    self.connected = True
                    async for raw in ws:
                        try:
                            self.handle(json.loads(raw))
                        except Exception as e:  # one odd record must not stop the feed
                            self.log_line(Text(f"(couldn't show a record: {e})", style="red"))
            except (OSError, websockets.WebSocketException):
                pass
            if self.connected:
                self.log_line(Text("(lost IO; reconnecting)", style="red"))
            self.connected = False
            self.streams.clear()
            self.dirty = True
            await asyncio.sleep(3)

    def handle(self, r: dict) -> None:
        ev = r.get("event")
        who = f"[{r['agent_label'][:40]}] " if r.get("agent_label") else ""
        if ev == "stream":
            self.on_stream(r)
        elif ev == "hello":
            self.effort = r.get("effort") or self.effort
            self.task_id = r.get("task_id") or ""
            if r.get("task"):
                self.log_line(Text(f"(IO is working on: {short(r['task'], 200)})", style="grey58"))
        elif ev == "start":
            self.task_id = r.get("task_id", "")
            self.log_line(Text(f"\n━━ {short(r.get('task', ''), 300)}", style="bold"))
        elif ev == "effort":
            self.log_line(Text(f"   effort {r.get('level')} · brain {r.get('brain')} · reasoning {r.get('reasoning_api') or '-'} (API) / "
                               f"{r.get('reasoning_local')} (local) · up to {r.get('steps')} steps", style="grey58"))
        elif ev == "llm":
            ok = r.get("ok")
            tokens = f" · {r.get('in_tokens')}→{r.get('out_tokens')} tokens" if r.get("in_tokens") is not None else ""
            waited = f" (waited {r['wait']}s)" if r.get("wait") else ""
            line = (f"   ⇄ {who}{r.get('model')} · {r.get('purpose')} · {r.get('secs')}s{waited}{tokens}"
                    + (f" · reasoning {r['reasoning']}" if r.get("reasoning") else "")
                    + (f" · {r['finish']}" if r.get("finish") and r.get("finish") != "stop" else "")
                    + ("" if ok else f" · {short(r.get('error', 'failed'), 120)}"))
            self.log_line(Text(line, style="green" if ok else "red"))
        elif ev == "race":
            self.log_line(Text(f"   ⚑ race won by {r.get('winner')} in {r.get('secs')}s", style="cyan"))
        elif ev == "tool":
            result = str(r.get("result", ""))
            bad = result.startswith(("error", "unsure")) or "refused" in result[:80]
            self.log_line(Text(f" ▸ {who}{r.get('name')}({short(r.get('args', {}), 220)})", style="yellow"))
            body = "\n".join("     " + ln for ln in result.splitlines()[:6])
            more = len(result.splitlines()) - 6
            self.log_line(Text(body + (f"\n     … {more} more lines" if more > 0 else ""), style="red" if bad else "white"))
        elif ev == "parallel":
            self.log_line(Text(f"   ∥ at once: {', '.join(r.get('names') or [])}", style="grey58"))
        elif ev == "plan":
            self.log_line(Text("   ☰ plan" + (f" ({r['source']})" if r.get("source") else "") + ":\n" +
                               "\n".join("     " + ln for ln in str(r.get("plan") or r.get("text") or "").splitlines()[:12]), style="cyan"))
        elif ev == "todo":
            items = r.get("items") or []
            done = sum(1 for i in items if i.get("status") == "done")
            now = next((i.get("text") for i in items if i.get("status") == "in_progress"), "")
            self.log_line(Text(f"   ☐ {done}/{len(items)} done" + (f" · now: {short(now, 120)}" if now else ""), style="grey58"))
        elif ev == "warning":
            self.log_line(Text(f"   ! {who}{short(r.get('text', ''), 300)}", style="dark_orange"))
        elif ev == "check":
            self.log_line(Text(f"   ✓? check: {short(r.get('text', ''), 300)}", style="magenta"))
        elif ev == "done":
            self.log_line(Text(f"\n✓ {r.get('summary') or ''}\n", style="bold green"))
            self.task_id = ""
        elif ev in ("agents", "agent_done", "ultra"):
            self.log_line(Text(f"   ⧉ {ev} {short({k: v for k, v in r.items() if k not in ('t', 'event', 'task_id')}, 260)}", style="cyan"))
        elif ev == "think":
            pass  # the stream already showed it, word by word
        elif ev not in ("step", "screenshot"):
            self.log_line(Text(f"   · {ev} {short({k: v for k, v in r.items() if k not in ('t', 'event', 'task_id')}, 200)}", style="grey50"))

    def on_stream(self, r: dict) -> None:
        rid, kind = r.get("rid", ""), r.get("kind")
        if kind == "start":
            label = f"{r.get('model', '?')} · {r.get('purpose', '')}" + (f" · reasoning {r['reasoning']}" if r.get("reasoning") else "")
            if r.get("agent"):
                label = f"[{r['agent'][:40]}] " + label
            self.streams[rid] = {"label": label, "think": "", "text": "", "tool": "", "args": "", "t0": time.time()}
        elif rid in self.streams:
            s = self.streams[rid]
            if kind == "thinking":
                s["think"] += r.get("text", "")
            elif kind == "text":
                s["text"] += r.get("text", "")
            elif kind == "tool":
                s["tool"] += r.get("text", "")
            elif kind == "args":
                s["args"] += r.get("text", "")
            elif kind == "end":
                self.streams.pop(rid, None)
                if r.get("error"):
                    why = "cut off: another model answered first" if "Connection" in r["error"] else r["error"]
                    self.log_line(Text(f"   ◇ {s['label']}: {why}", style="grey50"))
                else:
                    self.log_line(Text(f"   ◆ {s['label']} · {time.time() - s['t0']:.1f}s", style="bold cyan"))
                    if s["think"].strip() and not self.quiet:
                        self.log_line(Text("\n".join("     " + ln for ln in s["think"].strip().splitlines()), style="italic grey58"))
                    if s["text"].strip():
                        self.log_line(Text("     " + s["text"].strip().replace("\n", "\n     ")))
        self.dirty = True

    # ------------------------------------------------------------------------------------------------ input

    async def on_input_submitted(self, message: Input.Submitted) -> None:
        text = message.value.strip()
        message.input.value = ""
        if not text:
            return
        try:
            if text.startswith("/"):
                await self.command(text)
            else:
                await self.send(text)
        except urllib.error.URLError as e:
            self.log_line(Text(f"IO didn't answer: {e.reason if hasattr(e, 'reason') else e}", style="red"))
        except Exception as e:
            self.log_line(Text(f"{type(e).__name__}: {e}", style="red"))

    async def send(self, text: str) -> None:
        if not self.chat_id:
            chat = await asyncio.to_thread(http, "POST", "/api/chats", {})
            self.chat_id = chat.get("id") or (chat.get("chat") or {}).get("id") or ""
        self.log_line(Text(f"\nyou › {text}", style="bold"))
        await asyncio.to_thread(http, "POST", f"/api/chats/{self.chat_id}/messages", {"text": text, "effort": self.effort})

    async def command(self, text: str) -> None:
        name, _, rest = text[1:].partition(" ")
        name, rest = name.lower(), rest.strip()
        if name in ("help", "?"):
            self.log_line(Text(HELP, style="grey70"))
        elif name in ("quit", "exit"):
            self.exit()
        elif name == "clear":
            self.query_one("#log", RichLog).clear()
        elif name == "quiet":
            self.quiet = not self.quiet
            self.log_line(Text("(whole thoughts no longer go into the log)" if self.quiet else "(whole thoughts go into the log again)", style="grey58"))
        elif name == "effort":
            if rest.lower() in EFFORTS:
                self.effort = rest.lower()
                self.log_line(Text(f"(your next messages run at {self.effort})", style="grey58"))
            else:
                self.log_line(Text("effort is one of: " + ", ".join(EFFORTS), style="red"))
        elif name == "new":
            self.chat_id = ""
            self.log_line(Text("(a new chat starts with your next message)", style="grey58"))
        elif name == "chat":
            self.chat_id = rest
            self.log_line(Text(f"(your messages now go to chat {rest})", style="grey58"))
        elif name == "stop":
            if self.task_id:
                await asyncio.to_thread(http, "POST", f"/api/tasks/{self.task_id}/stop", {})
                self.log_line(Text(f"(asked IO to stop task {self.task_id})", style="grey58"))
            else:
                self.log_line(Text("(nothing is running)", style="grey58"))
        elif name == "tools":
            tools = (await asyncio.to_thread(http, "GET", "/api/console/tools")).get("tools", [])
            word = rest.lower()
            shown = [t for t in tools if not word or word in f"{t['name']} {t['group']} {t['summary']}".lower()]
            for g in sorted({t["group"] for t in shown}):
                self.log_line(Text(g, style="bold"))
                for t in sorted((t for t in shown if t["group"] == g), key=lambda t: t["name"]):
                    self.log_line(Text(f"  {t['signature']}: {t['summary']}"))
            if not shown:
                self.log_line(Text(f"no tool about {rest!r}", style="grey58"))
        elif name == "tool":
            tool, _, argtext = rest.partition(" ")
            args = tool_args(argtext)
            self.log_line(Text(f"\nyou › {tool}({short(args, 300)})", style="bold yellow"))
            r = await asyncio.to_thread(http, "POST", "/api/console/tool", {"name": tool, "args": args})
            result = str(r.get("result", ""))
            self.log_line(Text(result, style="red" if result.startswith("error") else "white"))
        else:
            self.log_line(Text(f"no command /{name}; /help lists them", style="red"))


if __name__ == "__main__":
    IOConsole().run()
