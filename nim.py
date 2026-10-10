"""NVIDIA's API catalog as IO's brain: an official OpenAI-compatible endpoint (no browser window) with GLM-5.3 Flash,
DeepSeek V4.1 Flash and Kimi K3, 40 requests a minute. The key lives in data/nim_key.txt (you paste it there or in
Settings); IO never prints it."""
import asyncio
import re
import threading
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
BRAIN_MODELS = ["z-ai/glm-5.3-flash", "moonshotai/kimi-k3", "meta/llama-3.2-90b-vision-instruct",
                "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning", "deepseek-ai/deepseek-v4.1-flash"]
# measured 2026-10-03 with a screenshot and a click tool: GLM-5.3 Flash 4-6 s, Kimi K3 11 s, Llama 3.2 90B Vision 4-5 s,
# Nemotron 3 Nano Omni 4 s (sometimes "worker limit reached"); all four answered with the right tool call. DeepSeek reads
# text only and queues for minutes, so it is last. (GLM-5.3 full refused images; Gemma 4 31B timed out; Kimi K2.6 is gone.)
ONE_IMAGE = {"meta/llama-3.2-90b-vision-instruct"}  # takes one picture per request: older ones become a note
BRAIN_LABELS = {"z-ai/glm-5.3-flash": "GLM-5.3 Flash", "deepseek-ai/deepseek-v4.1-flash": "DeepSeek V4.1 Flash",
                "moonshotai/kimi-k3": "Kimi K3", "meta/llama-3.2-90b-vision-instruct": "Llama 3.2 90B Vision",
                "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning": "Nemotron 3 Nano Omni"}
# what each is good at, for ask_model's menu (the brain picks a helper by these, not by a hardcoded rule)
STRENGTHS = {"moonshotai/kimi-k3": "strongest planner of the fast ones; best for 'what should I do next' and game strategy",
             "z-ai/glm-5.3-flash": "good all-rounder; reads screenshots well",
             "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning": "quickest; fine for simple yes/no questions about the screen",
             "meta/llama-3.2-90b-vision-instruct": "older but quick; describes screenshots plainly",
             "deepseek-ai/deepseek-v4.1-flash": "deep reasoning on text, but queues for minutes: only when time doesn't matter"}
# strongest planner first: after a failed step the next decision goes down this list instead of racing (a race is won by
# the quickest model, often a small one, which kept retrying a broken approach on a hard build)
PLANNER_ORDER = ["moonshotai/kimi-k3", "z-ai/glm-5.3-flash", "deepseek-ai/deepseek-v4.1-flash", "meta/muse-glimmer-30b",
                 "meta/llama-3.2-90b-vision-instruct", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"]


def planner_rank(model: str) -> int:
    """By the model itself, wherever it's served: Synthetic's "hf:moonshotai/Kimi-K3" ranks as NVIDIA's Kimi K3."""
    base = str(model).rsplit("/", 1)[-1].lower()
    return next((i for i, m in enumerate(PLANNER_ORDER) if m.rsplit("/", 1)[-1].lower() == base), len(PLANNER_ORDER))


# context windows in tokens, where known; anything else is assumed to take CONTEXT_DEFAULT. A race sends the same
# conversation to several models, so the brain's budget is the smallest window among them
CONTEXT = {"meta/llama-3.2-90b-vision-instruct": 131072}
CONTEXT_DEFAULT = 131072


def context_tokens(model: str) -> int:
    t = tests().get(model) or {}
    known = provider_model(model) if provider_of(model) else None
    return int(t.get("context") or (known or {}).get("context") or CONTEXT.get(model) or CONTEXT_DEFAULT)


NEEDS_REQUIRED_TOOLS = {"moonshotai/kimi-k3"}
NO_REQUIRED_TOOLS = {"deepseek-ai/deepseek-v4.1-flash"}  # its queue never answered a tool_choice="required" request
# Ultracode: never more than this many requests to NVIDIA in flight at once, from all of IO (sub-agents, the main brain,
# look_at_screen); the user's cap, well under the 40-a-minute limit
MAX_PARALLEL = 5
PER_MINUTE = 36  # requests started in any 60 s, from all of IO (NVIDIA allows 40)
SLOT_WAIT = 330  # seconds to wait for a free slot before giving up on this request (longer than any request's timeout)
_slots = threading.BoundedSemaphore(MAX_PARALLEL)
_sent: list[float] = []
_sent_lock = threading.Lock()


def _pace() -> None:
    """Waits until a request fits in the per-minute budget, then books it."""
    while True:
        with _sent_lock:
            now = time.time()
            _sent[:] = [t for t in _sent if now - t < 60]
            if len(_sent) < PER_MINUTE:
                _sent.append(now)
                return
            wait = 60 - (now - _sent[0]) + 0.05
        time.sleep(wait)


SLOW_BRAIN = 45.0  # seconds: a brain model averaging more than this goes to the back of the line while others are quicker
_health: dict = {}  # model -> {"avg": running reply time, "failed": when it last failed}
_orders = [0]


def note(model: str, secs: float, ok: bool, gone: bool = False) -> None:
    h = _health.setdefault(model, {"avg": None, "failed": 0.0})
    if ok:
        h["avg"] = secs if h["avg"] is None else 0.6 * h["avg"] + 0.4 * secs
    else:
        h["failed"] = time.time() + (480 if gone else 0)  # "not found" can be a blip (Muse Glimmer 404d once, then worked)


def brain_order(start: int, models: list[str] | None = None) -> list[int]:
    """Indices into `models` (BRAIN_MODELS by default) in the order to try: from `start` round the list, with models that
    failed in the last 2 minutes or have turned slow moved to the back. Every 8th call keeps the plain order, so a slow
    model whose queue has cleared gets measured again."""
    models = models or BRAIN_MODELS
    n = len(models)
    base = [(start + k) % n for k in range(n)]
    _orders[0] += 1
    if _orders[0] % 8 == 0:
        return base
    now = time.time()

    def fit(i: int) -> bool:
        h = _health.get(models[i], {})
        return now - h.get("failed", 0) > 120 and (h.get("avg") or 0) <= SLOW_BRAIN

    good = [i for i in base if fit(i)]
    return good + [i for i in base if i not in good]


listeners: list = []  # called with a timing record for every request (IO's debug timeline)


def size_of(messages) -> tuple[int, int]:
    """(characters of text, pictures) in a request's messages."""
    chars = images = 0
    for m in messages or []:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for p in c:
                if p.get("type") == "image_url":
                    images += 1
                else:
                    chars += len(str(p.get("text") or ""))
    return chars, images


def trace(record: dict) -> None:
    for f in listeners:
        try:
            f(record)
        except Exception:
            pass


# ---------- live streams (IO Console): while a console listens, model calls stream, and their thinking, answer and
# tool-call arguments go to stream_listeners piece by piece. The call still returns one finished completion, as before,
# so nothing else in IO changes; with no console open, nothing streams at all.
stream_listeners: list = []
context_label = lambda: {}  # noqa: E731  boss sets it: which Ultracode helper is asking, if any


def emit(record: dict) -> None:
    for f in list(stream_listeners):
        try:
            f(record)
        except Exception:
            pass


def call(client, label: dict, **kw):
    """client.chat.completions.create, streamed piece by piece to IO Console when one is open."""
    if not stream_listeners or kw.get("stream"):
        return client.chat.completions.create(**kw)
    return create_streamed(client, {**label, **context_label()}, **kw)


def create_streamed(client, label: dict, **kw):
    """client.chat.completions.create with stream=True, put back together as one ChatCompletion (content, the model's
    reasoning_content, tool calls, finish reason and usage), emitting each piece as it arrives."""
    from openai.types.chat import ChatCompletion  # noqa: PLC0415
    rid = f"{time.time():.3f}-{id(kw) % 10000}"
    text: list[str] = []
    think: list[str] = []
    calls: dict[int, dict] = {}
    finish, usage, model, cid, created = None, None, kw.get("model", ""), "", int(time.time())
    emit({"kind": "start", "rid": rid, **label})
    try:
        for chunk in client.chat.completions.create(**kw, stream=True, stream_options={"include_usage": True}):
            cid, model, created = chunk.id or cid, chunk.model or model, chunk.created or created
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            d = choice.delta
            extra = getattr(d, "model_extra", None) or {}
            r = extra.get("reasoning_content") or extra.get("reasoning")
            if r:
                think.append(r)
                emit({"kind": "thinking", "rid": rid, "text": r})
            if d.content:
                text.append(d.content)
                emit({"kind": "text", "rid": rid, "text": d.content})
            for tc in d.tool_calls or []:
                slot = calls.setdefault(tc.index or 0, {"id": "", "name": "", "args": ""})
                slot["id"] = tc.id or slot["id"]
                if tc.function and tc.function.name:
                    slot["name"] += tc.function.name
                    emit({"kind": "tool", "rid": rid, "text": tc.function.name})
                if tc.function and tc.function.arguments:
                    slot["args"] += tc.function.arguments
                    emit({"kind": "args", "rid": rid, "text": tc.function.arguments})
            finish = choice.finish_reason or finish
    except Exception as e:
        emit({"kind": "end", "rid": rid, "error": type(e).__name__})
        raise
    emit({"kind": "end", "rid": rid, "finish": finish or ""})
    message: dict = {"role": "assistant", "content": "".join(text) or None}
    if think:
        message["reasoning_content"] = "".join(think)
    if calls:
        message["tool_calls"] = [{"id": c["id"] or f"call_{i}", "type": "function",
                                  "function": {"name": c["name"], "arguments": c["args"] or "{}"}} for i, c in sorted(calls.items())]
    if finish not in ("stop", "length", "tool_calls", "content_filter", "function_call"):
        finish = "tool_calls" if calls else "stop"
    return ChatCompletion.model_validate({"id": cid or rid, "object": "chat.completion", "created": created, "model": model,
                                          "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                                          **({"usage": usage.model_dump()} if usage else {})})


def create(client, _purpose: str = "", **kw):
    """client.chat.completions.create, holding one of the MAX_PARALLEL slots while the request is out, and paced under
    the per-minute limit. Each request is reported to `listeners`: how long it waited for IO's own limits, how long
    the model took, and how big it was. A "<provider>:<id>" model (PROVIDERS) goes to that provider instead, whatever
    client the caller had."""
    t0 = time.time()
    chars, images = size_of(kw.get("messages"))
    rec = {"model": kw.get("model", ""), "purpose": _purpose, "chars": chars, "images": images, "tools": len(kw.get("tools") or [])}
    if label := reasoning_label(kw):
        rec["reasoning"] = label
    prov = provider_of(rec["model"])
    if prov:
        client, kw = provider_client(prov), provider_request(prov, kw)
    slots = _model_slots(rec["model"]) if prov else _slots
    if not slots.acquire(timeout=SLOT_WAIT):
        trace({**rec, "ok": False, "wait": round(time.time() - t0, 1), "secs": 0, "error": "no free slot"})
        raise TimeoutError(f"{label_of_provider(prov)}'s request slots stayed busy for {SLOT_WAIT}s")
    try:
        if not prov:
            _pace()  # NVIDIA's 40 a minute; the others limit per model, in their own ways
        t1 = time.time()
        try:
            r = call(client, {"model": rec["model"], "purpose": _purpose, **({"reasoning": rec["reasoning"]} if rec.get("reasoning") else {})}, **kw)
        except Exception as e:
            trace({**rec, "ok": False, "wait": round(t1 - t0, 1), "secs": round(time.time() - t1, 1),
                   "error": f"{type(e).__name__}: {scrub(str(e))[:120]}"})
            raise
        u = getattr(r, "usage", None)
        m = r.choices[0].message if r.choices else None
        extra = (getattr(m, "model_extra", None) or {}) if m is not None else {}
        trace({**rec, "ok": True, "wait": round(t1 - t0, 1), "secs": round(time.time() - t1, 1),
               "in_tokens": getattr(u, "prompt_tokens", None), "out_tokens": getattr(u, "completion_tokens", None),
               "calls": [c.function.name for c in (m.tool_calls or [])] if m else [], "finish": r.choices[0].finish_reason if r.choices else "",
               **({"thought_chars": n} if (n := len(str(extra.get("reasoning_content") or extra.get("reasoning") or ""))) else {})})
        return r
    finally:
        slots.release()


# ---------- other OpenAI-compatible providers. A brain model named "<provider>:<its own id>" is sent there by create(),
# so the brain, races, ask_model, look_at_screen and Settings' Test all work with it unchanged. Each key is pasted in
# Settings > Brain and kept in data/<provider>_key.txt.
#   Synthetic (synthetic.new), a flat monthly plan: GLM-5.3 and its Flash, Kimi K3, DeepSeek V4.1 Flash, Qwen 3.8 and
#     more. Measured 2026-10-09 on one real IO step (8.6K-token prompt, 67 tools): GLM-5.3 Flash 2-3 s and Kimi K3 2.5 s,
#     against 26 s and 205 s for the same models on NVIDIA's free queue. Its plan allows one request at a time per
#     model, and counts a request by the model's price (Kimi K3 = 1, GLM-5.3 Flash about 0.1).
#   Mistral (api.mistral.ai): limits per model (on a free key ministral-8b 188 a minute, ministral-14b 30; mistral-medium
#     and magistral 0 until the plan opens them); it takes tool-call ids of exactly 9 letters and digits.
PROVIDERS = {
    "synthetic": {"label": "Synthetic", "url": "https://api.synthetic.new/openai/v1", "site": "synthetic.new", "per_model": 1},
    "mistral": {"label": "Mistral", "url": "https://api.mistral.ai/v1", "site": "console.mistral.ai", "per_model": 4, "short_ids": True},
}
_prov: dict = {name: {"key": "", "client": None, "at": 0.0, "models": []} for name in PROVIDERS}
_model_sem: dict = {}
_prov_lock = threading.Lock()


def provider_of(model: str) -> str:
    """"synthetic" for "synthetic:hf:zai-org/GLM-5.3-Flash"; "" for NVIDIA's own models."""
    head, sep, _rest = str(model or "").partition(":")
    return head if sep and head in PROVIDERS else ""


def label_of_provider(name: str) -> str:
    return PROVIDERS[name]["label"] if name in PROVIDERS else "NVIDIA"


def provider_key(name: str) -> str:
    try:
        return (HERE / "data" / f"{name}_key.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_provider_key(name: str, key: str) -> None:
    f = HERE / "data" / f"{name}_key.txt"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(key.strip(), encoding="utf-8")
    _prov[name].update(key="", client=None, at=0.0, models=[])


def any_key() -> bool:
    """Whether any API brain can be used (NVIDIA's or another provider's key)."""
    return bool(nim_key()) or any(provider_key(n) for n in PROVIDERS)


def provider_client(name: str) -> OpenAI:
    key = provider_key(name)
    if not key:
        raise RuntimeError(f"no {PROVIDERS[name]['label']} key saved (Settings > Brain)")
    p = _prov[name]
    if p["client"] is None or p["key"] != key:
        p.update(key=key, client=OpenAI(base_url=PROVIDERS[name]["url"], api_key=key, max_retries=0, timeout=240))
    return p["client"]


def _model_slots(model: str) -> threading.BoundedSemaphore:
    with _prov_lock:
        if model not in _model_sem:
            _model_sem[model] = threading.BoundedSemaphore(PROVIDERS[provider_of(model)]["per_model"])
        return _model_sem[model]


def _short_id(call_id: str) -> str:
    """Mistral takes tool-call ids of exactly 9 letters and digits; ids other models made earlier in the conversation
    become such an id, the same one every time."""
    s = str(call_id or "")
    if re.fullmatch(r"[A-Za-z0-9]{9}", s):
        return s
    import hashlib  # noqa: PLC0415
    return hashlib.sha1(s.encode()).hexdigest()[:9]


def provider_request(name: str, kw: dict) -> dict:
    """The request as the provider takes it: its own model id, and messages with only the standard fields (no reasoning
    text another server put on them); short tool-call ids where the provider needs them."""
    out = {k: v for k, v in kw.items() if k != "extra_body"}
    out["model"] = str(kw["model"]).partition(":")[2]
    short = PROVIDERS[name].get("short_ids")
    messages = []
    for m in kw.get("messages") or []:
        if not isinstance(m, dict):
            m = m.model_dump(exclude_none=True) if hasattr(m, "model_dump") else dict(m)
        m = {k: v for k, v in m.items() if k in ("role", "content", "tool_calls", "tool_call_id", "name")}
        if short and m.get("tool_calls"):
            m["tool_calls"] = [{**c, "id": _short_id(c.get("id"))} for c in m["tool_calls"]]
        if short and m.get("tool_call_id"):
            m["tool_call_id"] = _short_id(m["tool_call_id"])
        messages.append(m)
    out["messages"] = messages
    return out


def provider_catalog(name: str) -> list[dict]:
    """The provider's chat models with tool calling for this key (cached 10 minutes): {"id": "<name>:<id>", "label",
    "vision", "efforts" (its reasoning levels, when it says), "context"}."""
    p = _prov[name]
    if time.time() - p["at"] < 600 and p["models"]:
        return p["models"]
    import json  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415
    req = urllib.request.Request(PROVIDERS[name]["url"] + "/models", headers={"Authorization": f"Bearer {provider_key(name)}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r).get("data", [])
    models, seen = [], set()
    for m in data:
        mid = str(m.get("id") or "")
        if not mid or mid in seen:
            continue
        if name == "mistral":
            caps = m.get("capabilities") or {}
            if not (caps.get("completion_chat") and caps.get("function_calling")) or re.search(r"-\d{4}$", mid):
                continue  # (dated copies like ministral-14b-2512 are the same model as its -latest name)
            entry = {"vision": bool(caps.get("vision")), "efforts": [], "context": m.get("max_context_length"),
                     "label": mid.replace("-latest", "").replace("-", " ").title()}
        else:  # Synthetic: its own models are "hf:<org>/<model>" ("syn:" names are aliases that move)
            if not mid.startswith("hf:") or "text" not in (m.get("output_modalities") or ["text"]):
                continue
            entry = {"vision": "image" in (m.get("input_modalities") or []),
                     "efforts": list((m.get("reasoning_parameters") or {}).get("efforts") or []),
                     "context": m.get("context_length"), "label": m.get("display_name") or mid.split("/")[-1]}
        seen.add(mid)
        models.append({"id": f"{name}:{mid}", **entry, "label": f"{entry['label']} ({PROVIDERS[name]['label']})"})
    p.update(at=time.time(), models=sorted(models, key=lambda x: x["id"]))
    return p["models"]


def provider_model(model: str, fetch: bool = False) -> dict | None:
    """What the provider's catalog says about one of its models. fetch: read the catalog now if it isn't (a model call,
    already in a worker thread); otherwise the page's state never waits on the network: the catalog is read in the
    background and the answer comes from the next call."""
    name = provider_of(model)
    if not name or not provider_key(name):
        return None
    p = _prov[name]
    if fetch or (p["models"] and time.time() - p["at"] < 600):
        try:
            return next((m for m in provider_catalog(name) if m["id"] == model), None)
        except Exception:
            return None
    if not p.get("loading"):
        def load() -> None:
            try:
                provider_catalog(name)
            except Exception:
                pass
            finally:
                p["loading"] = False
        p["loading"] = True
        threading.Thread(target=load, daemon=True).start()
    return next((m for m in p["models"] if m["id"] == model), None)  # the last catalog read, if any


def provider_effort(efforts: list, level: str) -> str:
    """IO's level (low / medium / high / max) in a model's own reasoning levels, as its provider lists them."""
    if not efforts:
        return ""
    want = {"low": ["low", "none"], "medium": ["medium", "high", "low"], "high": ["high", "xhigh", "medium"],
            "max": ["max", "xhigh", "high"]}.get(level, [level])
    return next((e for e in want if e in efforts), efforts[-1] if level in ("high", "max") else efforts[0])

# How much each model reasons, in its own words (boss.EFFORT's low / medium / high / max). Models expose different
# switches, and a field one ignores another rejects, so each gets only what was measured to work for it (2026-10-09,
# ~230 requests: thinking tokens on the same puzzle at each setting). Every model returns its thinking in
# message.reasoning_content.
#   GLM-5.3 Flash: only reasoning_effort low / high / max mean anything ("medium" and "none" quietly mean max); low gave
#     38 thinking tokens against 189 by default; it can't be turned off
#   Kimi K3: "none" turns thinking off; low / high / max made no measurable difference, so it is off or on ("medium" is
#     rejected with a 400)
#   Muse Glimmer: low / medium / high / xhigh (low ~320 tokens, high ~700 on the same question); it never fully stops
#   Nemotron 3 Nano Omni: chat_template_kwargs enable_thinking false turns it off; nvext max_thinking_tokens is a hard
#     cap; reasoning_effort does nothing (and top-level thinking fields are rejected)
#   Llama 3.2 90B Vision never reasons; DeepSeek V4.1 Flash couldn't be measured (its queue outlasted every request),
#     and a field it rejects only comes back after the whole queue wait, so it gets nothing
REASONING_FIELDS: dict[str, dict[str, dict]] = {
    # High asks GLM for "high", not "max": at "max" on a 27K-token step it timed out at 165 s again and again (2026-10-09),
    # where "high" answered the same step in 17 s; Max keeps "max"
    "z-ai/glm-5.3-flash": {"low": {"reasoning_effort": "low"}, "medium": {"reasoning_effort": "high"},
                           "high": {"reasoning_effort": "high"}, "max": {"reasoning_effort": "max"}},
    "moonshotai/kimi-k3": {"low": {"reasoning_effort": "none"}, "medium": {}, "high": {}, "max": {"reasoning_effort": "max"}},
    "meta/muse-glimmer-30b": {"low": {"reasoning_effort": "low"}, "medium": {"reasoning_effort": "medium"},
                              "high": {"reasoning_effort": "high"}, "max": {"reasoning_effort": "xhigh"}},
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning": {
        "low": {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
        "medium": {"extra_body": {"nvext": {"max_thinking_tokens": 1024}}}, "high": {}, "max": {}},
}


def reasoning(model: str, level: str) -> dict:
    """Keyword arguments for chat.completions.create that ask `model` for `level` of reasoning ({} for a model with no
    switch IO knows, which then reasons as it does by default)."""
    if provider_of(model):  # its provider lists each model's own levels
        known = provider_model(model, fetch=True)
        effort = provider_effort(known["efforts"] if known else [], level)
        return {"reasoning_effort": effort} if effort else {}
    fields = REASONING_FIELDS.get(model, {}).get(level, {})
    return {k: (dict(v) if isinstance(v, dict) else v) for k, v in fields.items()}


def reasoning_label(kw: dict) -> str:
    """What a request asked for, for the debug timeline."""
    if kw.get("reasoning_effort"):
        return str(kw["reasoning_effort"])
    extra = kw.get("extra_body") or {}
    if (extra.get("chat_template_kwargs") or {}).get("enable_thinking") is False:
        return "off"
    if (extra.get("nvext") or {}).get("max_thinking_tokens"):
        return f"cap {extra['nvext']['max_thinking_tokens']}"
    return ""


TEXT_ONLY = {"deepseek-ai/deepseek-v4.1-flash"}  # gets the conversation without screenshots (it calls look_at_screen)
SLOW_QUEUE = {"deepseek-ai/deepseek-v4.1-flash"}  # queues for minutes: allowed a longer wait per request
VISION = {"z-ai/glm-5.3-flash", "moonshotai/kimi-k3", "meta/llama-3.2-90b-vision-instruct",
          "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"}  # measured: answer a screenshot with the right tool call

# ---------- the catalog and model tests (Settings > Brain) ----------
TESTS_FILE = HERE / "data" / "nim_models.json"  # {model: {"vision", "tools", "secs", "when", "note"}} from Settings' Test
NOT_CHAT = re.compile(r"embed|rerank|retriev|guard|safety|reward|parse|ocr|clip|tts|asr|whisper|canary|riva|translat|"
                      r"cosmos|flux|stable-diffusion|sdxl|bge|e5-|nv-embed|nemoretriever|grounding|detector|segment", re.I)
_catalog: dict = {"at": 0.0, "ids": []}


def tests() -> dict:
    try:
        import json
        return json.loads(TESTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def label(model: str) -> str:
    if model in BRAIN_LABELS:
        return BRAIN_LABELS[model]
    if provider_of(model):
        known = provider_model(model)
        return known["label"] if known else model.partition(":")[2].split("/")[-1].replace("-latest", "") + f" ({label_of_provider(provider_of(model))})"
    name = model.split("/")[-1].replace("-instruct", "").replace("-it", "")
    return name.replace("-", " ").replace("_", " ").title()


def is_vision(model: str) -> bool:
    """Whether the brain may send this model screenshots: measured here, or passed Settings' Test with a picture."""
    t = tests().get(model)
    if t and "vision" in t:
        return bool(t["vision"])
    if provider_of(model):  # its provider's catalog says
        known = provider_model(model)
        return bool(known and known["vision"])
    return model in VISION


def catalog(key: str = "") -> list[str]:
    """The chat models NVIDIA's catalog offers this key (cached 10 minutes)."""
    if time.time() - _catalog["at"] < 600 and _catalog["ids"]:
        return _catalog["ids"]
    import json
    import urllib.request
    req = urllib.request.Request(NIM_URL + "/models", headers={"Authorization": f"Bearer {key or nim_key()}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        ids = sorted({m.get("id") for m in json.load(r).get("data", []) if isinstance(m, dict) and m.get("id")})
    _catalog.update(at=time.time(), ids=[i for i in ids if not NOT_CHAT.search(i)])
    return _catalog["ids"]


def test_model(model: str) -> dict:
    """Shows the model a small game screenshot with a click tool, then the same without the picture: whether it sees,
    whether it calls tools, and how long it took. Saved for is_vision() and Settings."""
    import base64
    import io
    import json
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (640, 360), (30, 30, 60))
    d = ImageDraw.Draw(im)
    d.rectangle([240, 150, 400, 210], fill=(40, 160, 70))
    d.text((290, 172), "COLLECT", fill="white")
    d.text((220, 90), "Offline gains: 1,250 gold", fill="white")
    buf = io.BytesIO()
    im.save(buf, format="JPEG")
    url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    tools = [{"type": "function", "function": {"name": "click_on", "description": "Click a described element on screen",
              "parameters": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}}}]
    client = OpenAI(base_url=NIM_URL, api_key=nim_key(), max_retries=0, timeout=90)
    result = {"vision": False, "tools": False, "secs": None, "when": time.time(), "note": ""}

    def ask(content, use_tools=True):
        kw = {"tools": tools, "tool_choice": "required" if model in NEEDS_REQUIRED_TOOLS else "auto"} if use_tools else {}
        t0 = time.time()
        r = create(client, model=model, temperature=0.2, max_tokens=600,
                   messages=[{"role": "system", "content": "You play a game through tools. Act with tool calls."},
                             {"role": "user", "content": content}], **kw)
        return r.choices[0].message, time.time() - t0

    try:
        m, secs = ask([{"type": "image_url", "image_url": {"url": url}}, {"type": "text", "text": "Current screenshot. Do the next useful action."}])
        result["secs"] = round(secs, 1)
        calls = m.tool_calls or []
        if calls:
            result["tools"] = True
            result["vision"] = "collect" in (calls[0].function.arguments or "").lower()
            result["note"] = f"{calls[0].function.name}({calls[0].function.arguments})"[:120]
        else:
            result["vision"] = "collect" in (m.content or "").lower()
            result["note"] = "no tool call: " + (m.content or "")[:100]
    except Exception as e:  # a model that refuses pictures: try it as a text-only brain
        result["note"] = f"with a picture: {type(e).__name__}: {scrub(str(e))[:120]}"
        try:
            m, secs = ask("A button labelled COLLECT is on the screen. Do the next useful action.")
            result["secs"] = round(secs, 1)
            result["tools"] = bool(m.tool_calls)
            result["note"] += "; text only: " + ("tool call works" if m.tool_calls else "no tool call")
        except Exception as e2:
            result["note"] += f"; text only: {type(e2).__name__}: {scrub(str(e2))[:120]}"
    saved = tests()
    saved[model] = result
    TESTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TESTS_FILE.write_text(json.dumps(saved, indent=1), encoding="utf-8")
    return result
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
    for key in (nim_key(), *(provider_key(n) for n in PROVIDERS)):
        if key and len(key) > 8:
            text = text.replace(key, "***")
    return text


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
