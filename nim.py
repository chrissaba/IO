"""GLM-5.3 Flash on NVIDIA's API catalog as IO's director: an official OpenAI-compatible endpoint (no browser window), with
images, a 1M-token context and 40 requests a minute. The key lives in data/nim_key.txt (you paste it there or in Settings);
IO never prints it."""
import asyncio
import re
import time
from pathlib import Path

from openai import APIStatusError, OpenAI

HERE = Path(__file__).parent
KEY_FILE = HERE / "data" / "nim_key.txt"
NIM_URL = "https://integrate.api.nvidia.com/v1"
NIM_MODEL = "z-ai/glm-5.3-flash"
# NVIDIA's free catalog models that can see screenshots, best director first (measured 2026-10-03 with a two-shape test
# picture: all of these named both shapes; median reply time on the free queue in brackets). Queues vary a lot, so IO
# keeps a running average per model and skips one whose recent replies are slow, coming back to it now and then.
NIM_MODELS = [
    ("moonshotai/kimi-k3", "Kimi K3"),                                          # ~11 s, strongest planner of the fast ones
    ("z-ai/glm-5.3-flash", "GLM-5.3 Flash"),                                    # ~25 s (10-40)
    ("nvidia/nemotron-3-nano-omni-30b-a3b-reasoning", "Nemotron 3 Nano Omni"),  # ~4 s, smaller
    ("meta/llama-3.2-90b-vision-instruct", "Llama 3.2 90B Vision"),             # ~9 s, older
]
# the single brain, tried in this order (the user's pick; speed doesn't matter, the next one takes over on an error):
# GLM-5.3 Flash (tools + images), DeepSeek V4.1 Flash (tools, text only, very slow queue: ~170 s a turn), Kimi K3 (tools
# only with tool_choice="required": on "auto" it returns nothing)
BRAIN_MODELS = ["z-ai/glm-5.3-flash", "deepseek-ai/deepseek-v4.1-flash", "moonshotai/kimi-k3"]
NEEDS_REQUIRED_TOOLS = {"moonshotai/kimi-k3"}
TEXT_ONLY = {"deepseek-ai/deepseek-v4.1-flash"}  # gets the conversation without screenshots (it calls look_at_screen)
SLOW_AFTER = 20.0  # seconds: a model averaging more than this is passed over while a quicker one is available
RETRY_SLOW_EVERY = 6  # rounds: then the best model gets another chance (queues clear)
KEEP_TURNS = 24  # conversation turns kept (about 12 rounds); older ones are dropped, the brief and plan carry the gist
KEEP_IMAGES = 2  # screenshots kept in the conversation: older ones become "[earlier screenshot]" to keep requests small


def nim_key() -> str:
    try:
        return KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_nim_key(key: str) -> None:
    """Saves the key (an empty one clears it)."""
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(key.strip(), encoding="utf-8")


def scrub(text: str) -> str:
    """Text with the key masked, for anything that may reach a log or the screen."""
    key = nim_key()
    return text.replace(key, "***") if key and len(key) > 8 else text


class NimDirector:
    """Same interface IO's director uses for Duck.ai: ask(prompt, image, keep, instructions) -> reply text or 'error: ...'.
    The conversation is kept here, client side, so it never hits a web chat's picture or length limits."""

    takes_images, private, conversational = True, True, True
    max_chars = 30000  # a message, not the model's limit: keeps each round quick
    images = 0  # no per-conversation picture cap here (IO's rollover logic reads it)

    def __init__(self, key: str = "") -> None:
        self.key = key or nim_key()
        self.client = OpenAI(base_url=NIM_URL, api_key=self.key or "missing", max_retries=1, timeout=120)
        self.history: list[dict] = []
        self.system = ""
        self.in_chat = False
        self.avg = {m: None for m, _ in NIM_MODELS}  # running reply time per model; None = not tried yet
        self.failed: dict[str, float] = {}  # model -> when it last failed (skipped for a while)
        self.rounds = 0
        self.model = NIM_MODELS[0][0]  # the one that answered last (for the log)

    def _pick(self) -> list[str]:
        """Models in the order to try this round: the best one that isn't slow or recently failed first."""
        now = time.time()
        usable = [m for m, _ in NIM_MODELS if now - self.failed.get(m, 0) > 300]
        if not usable:
            usable = [m for m, _ in NIM_MODELS]
        if self.rounds % RETRY_SLOW_EVERY == 0:
            return usable  # every few rounds, best first regardless of speed
        quick = [m for m in usable if self.avg[m] is None or self.avg[m] <= SLOW_AFTER]
        return quick + [m for m in usable if m not in quick]

    def name(self) -> str:
        return dict(NIM_MODELS).get(self.model, self.model)

    def ready_in(self) -> int:
        return 0

    def _trim(self) -> None:
        self.history = self.history[-KEEP_TURNS:]
        while self.history and self.history[0]["role"] != "user":
            self.history.pop(0)
        seen = 0
        for m in reversed(self.history):
            if isinstance(m["content"], list):
                if seen >= KEEP_IMAGES:
                    m["content"] = [p if p.get("type") == "text" else {"type": "text", "text": "[earlier screenshot]"} for p in m["content"]]
                seen += any(p.get("type") == "image_url" for p in m["content"])

    def _ask(self, prompt: str, image: bytes, keep: bool, instructions: str) -> str:
        if not self.key:
            return "error: no NVIDIA API key (paste it into data/nim_key.txt or Settings)"
        if not (keep and self.in_chat):
            self.history, self.system = [], instructions or self.system
        content = prompt
        if image:
            import base64
            content = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(image).decode()}},
                       {"type": "text", "text": prompt}]
        self.history.append({"role": "user", "content": content})
        self._trim()
        messages = ([{"role": "system", "content": self.system}] if self.system else []) + self.history
        self.rounds += 1
        text, last_error = "", "error: no NVIDIA model answered"
        for model in self._pick():
            t0 = time.time()
            try:
                reply = self.client.chat.completions.create(model=model, messages=messages, temperature=0.2, max_tokens=1500,
                                                            timeout=60 if self.avg[model] is None else max(30, self.avg[model] * 3))
                text = re.sub(r"<think>.*?</think>", "", reply.choices[0].message.content or "", flags=re.S).strip()
            except APIStatusError as e:
                last_error = ("error: limit: NVIDIA says too many requests" if e.status_code == 429
                              else f"error: NVIDIA API {e.status_code} on {model}: {str(e)[:160]}")
            except Exception as e:
                last_error = f"error: NVIDIA API unreachable ({model}): {e}"[:300]
            took = time.time() - t0
            if text:
                self.avg[model] = took if self.avg[model] is None else 0.6 * self.avg[model] + 0.4 * took
                self.model = model
                break
            self.failed[model] = time.time()  # timed out, refused or empty: give the next model the round
            self.avg[model] = max(self.avg[model] or 0, took)
        if not text:
            self.history.pop()
            return last_error
        self.history.append({"role": "assistant", "content": text})
        self.in_chat = keep
        return text

    async def ask(self, prompt: str, image: bytes = b"", keep: bool = False, instructions: str = "") -> str:
        return await asyncio.to_thread(self._ask, prompt, image, keep, instructions)

    async def close(self) -> None:
        self.history, self.in_chat = [], False


if __name__ == "__main__":
    import io
    import json
    import sys

    from PIL import Image, ImageDraw

    im = Image.new("RGB", (400, 200), "white")
    ImageDraw.Draw(im).rectangle([150, 60, 250, 140], fill="red")
    buf = io.BytesIO()
    im.save(buf, format="JPEG")
    d = NimDirector()
    t = time.time()
    print(asyncio.run(d.ask('Reply ONLY with JSON {"seen": "<what the image shows>"}', buf.getvalue(), keep=True)))
    print(asyncio.run(d.ask('And what colour was it? JSON {"colour": "..."}', keep=True)))
    print(round(time.time() - t, 1), "s for two rounds", file=sys.stderr)
