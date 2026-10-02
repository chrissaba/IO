# IO

IO is a local desktop assistant for Windows, named after Jupiter's moon. A small local text model (the boss,
Gemma 4 E4B on llama.cpp) operates the PC through [Windows-MCP](https://github.com/CursorTouch/Windows-MCP),
using the accessibility tree to launch apps, click named controls, type and run PowerShell. UI-TARS-1.5-7B
(in Unsloth Studio) is its eyes: `find_on_screen` and `look_at_screen` handle what the accessibility tree
can't see. It is fully local: the boss also writes its own plans.

## Using it

IO is an installed per-user app: open it from the Start menu or Windows search, pin it to the taskbar, and
find it under Settings > Apps. It starts at sign-in in the tray when **Start with Windows** is on (tray menu).
Closing the window keeps it running in the tray, so queued and scheduled tasks keep going. On start it opens
Unsloth Studio, loads UI-TARS and starts the boss model as needed.

- **Chat** like Claude: each message runs as a task that sees the conversation's earlier messages, actions
  and attached images, with a live checklist of steps, Tab-to-complete suggestions and a "New" line for
  replies that arrived while you were away. Chats can be pinned, renamed and deleted from the title menu.
- **Browser**: Playwright MCP drives pages by their elements, either in a separate Edge window or in your
  own Chrome through IO's Chrome extension (`chrome-extension/`), where IO's tabs sit in a tab group named
  IO. The agent opens its tab with `browser_open` and reads long pages with `browser_read`.
- **Customize**: one-click plugins (MCP servers from `catalog.json`: web search, documents, Obsidian,
  GitHub, SQLite and more) and skills (your own instructions, added to tasks they fit).
- **Schedules** (every N minutes or daily), **triggers** (a new file in a folder, or
  `POST /api/hook/<name>`), **memory** notes, **history**, **usage**, pause/resume and notifications.
- **Safety**: risky actions (deleting files, killing processes, writing plugin tools, SQL that writes)
  wait for your OK; Ctrl+Alt+End stops everything. Ctrl+Alt+Q opens a new chat.

The window is frameless with its own title bar (`desktop.py` hands the caption to the page and keeps Windows'
snapping, resizing and shadow). The same panel is served at http://127.0.0.1:8765, and other programs can
queue work there:

```
curl -X POST http://127.0.0.1:8765/api/tasks -H "Content-Type: application/json" -d "{\"task\": \"open notepad and type hello\"}"
```

## Fully local

Everything runs on this PC: the boss plans each task itself before acting (and replans when stuck), and no
requests go to cloud models.

## Your Chrome (optional)

1. In Chrome, open `chrome://extensions`, turn on Developer mode, click **Load unpacked** and choose
   `chrome-extension/` (Settings > Browser has an "Open extension folder" button).
2. Click IO's icon in Chrome, copy its token, and paste it in Settings > Browser with "Your Chrome" selected.
3. Press Test: a tab opens in a group called IO.

`chrome-extension/` is a modified copy of Microsoft's Playwright Extension (Apache-2.0); see its NOTICE.

## Setup from scratch

Needs Windows 11, Node.js, [uv](https://docs.astral.sh/uv/), Unsloth Studio (with UI-TARS-1.5-7B Q8_0 and
its llama.cpp build) and the Gemma 4 E4B GGUF in the Hugging Face cache (see `start-boss-server.cmd`).

```
uv venv --python 3.14 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
cd mcp && npm install && cd ..
.venv\Scripts\pythonw.exe install.py
```

Run these from a normal terminal. A Python installed from inside another packaged app (for example the
Claude desktop app's terminal) lands in that app's private storage, where Windows can't start it from the
Start menu; `install.py` refuses in that case.

Run a task without the window: `.venv\Scripts\python.exe boss.py "open notepad and type hello"`. Each step is
logged to `logs/boss.jsonl`. `toolcheck.py` (Settings > Tools) tests every tool without touching the screen.

| Variable | Default |
| --- | --- |
| `BOSS_URL` / `BOSS_MODEL` | `http://127.0.0.1:8090/v1` / `boss` |
| `EYES_URL` / `EYES_MODEL` | `http://127.0.0.1:8888/v1` / `mradermacher/UI-TARS-1.5-7B-GGUF` |
| `STUDIO_API_KEY` | `data/studio.json`, else UI-TARS Desktop's saved settings |

`extras/ui-tars-desktop.patch` holds the changes made to [UI-TARS-desktop](https://github.com/bytedance/UI-TARS-desktop)
while testing it with Unsloth Studio (timeouts and retries, image size for UI-TARS-1.5, clipboard timing,
pulling windows to the primary monitor, snapping clicks to controls).

UI libraries vendored in `static/`: Alpine.js (MIT), Lucide (ISC), marked (MIT), DOMPurify (Apache-2.0/MPL),
Geist and Lora fonts (OFL).
