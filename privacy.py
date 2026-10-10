"""IO's privacy check and Private mode.

Before anything IO read on this PC goes to an API model (Claude, Synthetic, NVIDIA, Mistral), it passes the gate here:
an instant pattern scan (keys and tokens, card and bank numbers, ID numbers, chat logs, private-app screenshots), then
the local Muse model for longer text (documents, chats, health and work records). A hit pauses the task and asks:
send it anyway, or keep the chat on this PC. Keeping it local makes the chat private: no API calls at all for the rest
of it (the task starts over on the local model, which thinks at its highest level). Unattended runs (goals, schedules,
loops) don't wait for an answer: they go private by themselves.

Every API request goes through nim.create, and nim.create calls the gate first (nim.send_guards), so nothing a task
sends can go around it. Nothing in this file sends anything anywhere: the local model call is passed in by boss.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import re
import threading
import urllib.parse
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ALLOW_FILE = HERE / "data" / "privacy.json"  # content you said may go to the API (hashes only, never the content)

# what the check looks for (Settings > Privacy): key -> (label, on by default). Everyday contact details are off by
# default: Claude's and Synthetic's APIs keep nothing and never train, so the check stops for the kinds nobody would
# hand out, and asks rarely enough that a question means something
KINDS = {
    "secrets": ("Passwords, API keys and login tokens", True),
    "payment": ("Card and bank account numbers", True),
    "ids": ("Government ID numbers (SSN, passport, driver's license, tax ID)", True),
    "health": ("Health and medical information about a person", True),
    "chats": ("Private chats and emails (Discord, Slack, WhatsApp, texts, DMs, mail)", True),
    "work": ("Confidential work documents (contracts, client data, internal reports)", True),
    "contact": ("Home addresses, personal phone numbers and emails", False),
}
DEFAULT_KINDS = [k for k, (_label, on) in KINDS.items() if on]
# how the local model is told each kind
KIND_RULES = {
    "secrets": "a real password, API key, access token, private key or recovery code (not a placeholder like YOUR_KEY)",
    "payment": "a real payment card number, bank account number or IBAN, or a bank or card statement",
    "ids": "a government ID number: social security, passport, driver's license, national ID or tax ID",
    "health": "medical or health information about a person: diagnoses, prescriptions, lab results, surgeries, insurance claims",
    "chats": "a private conversation between people: lines of people talking to each other (\"Name: message\"), with or "
             "without times, from Discord, Slack, WhatsApp, Messenger, texts or DMs, or personal email",
    "work": "a confidential business document: contracts, client or employee records, internal financials or reports, "
            "anything marked confidential or internal",
    "contact": "a person's home address, personal phone number or personal email address",
}

MODEL_SYSTEM = """You guard the privacy of a Windows PC's owner. An assistant on the PC read the text below (a file, a
window, a web page or a command's output) and is about to send it to a cloud AI service. Say whether it contains any of
these kinds of private data:
{rules}
Not private: public web pages, news and documentation; source code, configs and logs (unless they hold a real password
or key); app menus, settings, window and file lists; game text; product pages; the assistant's own notes about its
work; made-up examples and placeholders; the owner's instructions to the assistant (but anything pasted into them is
judged like the rest).
Judge what is really in the text, not what it might be about. Reply with JSON only:
{{"found": [{{"kind": "one of {keys}", "what": "what it is, in under 10 words, without copying any name, number or value"}}]}}
or {{"found": []}} when there is none."""

QUERY_SYSTEM = """The owner of a Windows PC is in a private chat: what the assistant reads stays on the PC. The assistant
now wants to send the text below to a web search engine or website. Does it carry private details: a private person's
name, an account, card, ID or phone number, a home address, a health condition tied to someone, a password or key, or
the name of a confidential project or client? Public topics, products, software, places and famous people are fine.
Answer with one line: OK, or PRIVATE: what it carries (under 10 words, without copying the details)."""

# what makes the local model worth asking (it takes ~2 s a look): words and shapes the model-judged kinds come with. A
# text with none of them (a docs page, a build log, an app's menus) gets the pattern scan alone
SIGNALS = {
    "health": r"\b(patient|diagnos\w*|prescri\w*|medication|dosage|\d+ ?mg\b|symptom\w*|clinic|hospital|physician|doctor|dr\. [A-Z]|"
              r"therap\w*|surgery|lab results?|blood (test|pressure|work)|a1c|allerg\w*|health insurance|medicaid|medicare|"
              r"mental health|psychiatr\w*|pregnan\w*|dob\b|date of birth)",
    "work": r"\b(confidential|internal (only|use)|proprietary|do not (distribute|share|forward)|nda\b|non-disclosure|agreement|"
            r"contract|invoice|salar(y|ies)|payroll|compensation|revenue|forecast|budget|customer list|employee|personnel|"
            r"performance review|board meeting|acquisition|merger|attorney|privileged)",
    "chats": r"(^|\n)\s*(from|to|subject|cc):\s|\b(dm|dms|lol|lmao|brb|omg|replied|reacted|sent you|wrote:)\b",
    "contact": r"\b(address|apt\.?|suite|zip code|phone|cell|mobile)\b",
    "payment": r"\b(bank|account (number|no)|routing|statement|balance|credit card|debit card|card ending|cvv|iban|swift)\b",
    "ids": r"\b(passport|driver'?s licen[cs]e|social security|ssn|tax id|ein|national id|date of birth)\b",
    "secrets": r"\b(password|passcode|recovery code|2fa|backup codes?|seed phrase|private key|secret)\b",
}
SIGNAL_RES = {k: re.compile(v, re.I) for k, v in SIGNALS.items()}
# speakers: "Alex: did you see..." lines, with a name coming back, are a conversation whatever app it came from (a
# "Name: chrome / Id: 1234" listing has a new key on every line)
SPEAKER = re.compile(r"(?m)^[ \t]*\[?([A-Z][\w .'-]{0,24}?)\]?:[ \t]+\S")
NOT_SPEAKER = re.compile(r"(?i)^(info|debug|warn|warning|error|trace|fatal|critical|inf|dbg|wrn|err|vrb|note|tip|hint|"
                         r"name|id|type|status|path|url|title|value|result|output|file|line|step|time|date)$")

QUESTION = "Privacy check:"  # how its question starts (the app shows it with its own buttons)
PROVIDER_NAMES = {"anthropic": "Claude", "synthetic": "Synthetic", "mistral": "Mistral"}
MODEL_MIN = 160  # shorter text gets the pattern scan only ("ok: clicked Save" needs no model)
CHUNK = 8000  # characters per local-model look; a long text gets its first two chunks and its last one
MAX_CHUNKS = 3

# tools whose results are public or IO's own: the pattern scan still runs on them, the local model doesn't
PUBLIC_TOOLS = {"web_search", "web_answer", "research", "api_lookup", "about_io", "tools", "notes", "todo", "remember",
                "wait", "done", "propose_tool", "workshop_test", "tool_ready", "add_goal", "ask_model", "install_package"}
# source files: secrets in them are caught by the patterns; the code itself is nothing to ask about
CODE_EXT = {".py", ".pyw", ".cs", ".csproj", ".sln", ".props", ".targets", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
            ".html", ".htm", ".css", ".scss", ".xaml", ".xml", ".yml", ".yaml", ".toml", ".ps1", ".psm1", ".bat", ".cmd",
            ".sh", ".c", ".cc", ".cpp", ".h", ".hpp", ".java", ".kt", ".go", ".rs", ".rb", ".php", ".lua", ".sql", ".ini",
            ".cfg", ".lock", ".gitignore", ".editorconfig", ".svg"}
# web pages: public sites are public; these hold your own mail, chats, documents, money and health
PRIVATE_SITES = re.compile(r"(^|\.)(discord(app)?\.com|slack\.com|mail\.google\.com|docs\.google\.com|drive\.google\.com|"
                           r"outlook\.(live|office|office365)\.com|teams\.(microsoft|live)\.com|web\.whatsapp\.com|"
                           r"messenger\.com|web\.telegram\.org|mail\.proton\.me|mail\.yahoo\.com|icloud\.com|notion\.so|"
                           r"sharepoint\.com|onedrive\.live\.com|dropbox\.com|claude\.ai|chatgpt\.com|chat\.openai\.com|"
                           r"paypal\.com|venmo\.com|mychart|patient|bank|chase\.com|wellsfargo\.com|capitalone\.com|"
                           r"americanexpress\.com|discover\.com|citi\.com|usbank\.com|fidelity\.com|vanguard\.com|"
                           r"schwab\.com|robinhood\.com|coinbase\.com|irs\.gov|ssa\.gov)$", re.I)

PLACEHOLDER = re.compile(r"(?i)your|example|placeholder|x{4,}|\*{3,}|<|>|dummy|redacted|changeme|\.\.\.|…|\$\{|\{\{")
SECRET_RES = [(re.compile(p), what) for p, what in (
    (r"\bsk-ant-[A-Za-z0-9_-]{20,}", "an Anthropic API key"),
    (r"\bsk-(?!ant-)(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}", "an OpenAI-style API key"),
    (r"\bnvapi-[A-Za-z0-9_-]{20,}", "an NVIDIA API key"),
    (r"\bsyn_[A-Za-z0-9_-]{20,}", "a Synthetic API key"),
    (r"\bcfut_[A-Za-z0-9_-]{20,}", "a Cloudflare API token"),
    (r"\bhf_[A-Za-z0-9]{30,}", "a Hugging Face token"),
    (r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})", "a GitHub token"),
    (r"\bglpat-[A-Za-z0-9_-]{20,}", "a GitLab token"),
    (r"\bxox[abprs]-[A-Za-z0-9-]{10,}", "a Slack token"),
    (r"\bAKIA[0-9A-Z]{16}\b", "an AWS access key"),
    (r"\bAIza[0-9A-Za-z_-]{35}\b", "a Google API key"),
    (r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----", "a private key"),
    (r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", "a login token (JWT)"),
    (r"(?<![\w.-])[MNO][A-Za-z0-9_-]{23,27}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,40}(?![\w.-])", "a Discord token"),
    # a password on a line of its own (a config or a note), not code that computes one: password = input() is fine
    (r"(?im)^[ \t]*[\"']?(?:password|passwd|pwd|passcode)[\"']?[ \t]*[:=][ \t]*[\"']?[^\s\"'(){}\[\],;$%*]{4,}[\"']?[ \t]*,?[ \t]*$",
     "a password"),
    (r"(?i)\b(?:api[_-]?key|secret[_-]?key|client[_-]?secret|access[_-]?token|auth[_-]?token|refresh[_-]?token)[\"']?[ \t]*[:=][ \t]*"
     r"[\"'][A-Za-z0-9_\-./+=]{16,}[\"']", "a secret key"),
    (r"(?i)\bauthorization:[ \t]*bearer[ \t]+[A-Za-z0-9._~+/-]{20,}=*", "a bearer token"),
)]
CARD = re.compile(r"(?<![\w.-])\d(?:[ -]?\d){12,18}(?![\w-])")
# the payment processors' published test numbers (in docs, code and IO's own tests): nobody's card
TEST_CARDS = {"4111111111111111", "4242424242424242", "4012888888881881", "4000056655665556", "4000000000000002",
              "5555555555554444", "5105105105105100", "2223003122003222", "5200828282828210", "378282246310005",
              "371449635398431", "6011111111111117", "6011000990139424", "3056930009020004", "3566002020360505"}
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")
SSN = re.compile(r"(?<![\w-])(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?![\w-])")
SSN_LABELED = re.compile(r"(?i)\b(?:ssn|social security(?: number| no\.?| #)?)[ \t]*[:#]?[ \t]*\d{3}[- ]?\d{2}[- ]?\d{4}\b")
# chat logs as they're copied or exported: Discord ("name — Today at 3:45 PM"), DiscordChatExporter
# ("[10/9/2026 3:45 PM] name"), WhatsApp ("10/9/26, 15:45 - Name: text"), and "[12:34] name: text"
CHAT_LINES = [re.compile(p, re.M | re.I) for p in (
    r"^.{1,40} — (?:today|yesterday) at \d{1,2}:\d{2}\s?[ap]m\s*$",
    r"^.{1,40} — \d{1,2}/\d{1,2}/\d{2,4},? \d{1,2}:\d{2}\s?(?:[ap]m)?\s*$",
    r"^\[\d{1,2}/\d{1,2}/\d{2,4},? \d{1,2}:\d{2}(?::\d{2})?(?:\s?[ap]m)?\] .{1,40}",
    r"^\[?\d{1,2}/\d{1,2}/\d{2,4},? \d{1,2}:\d{2}(?::\d{2})?(?:\s?[ap]m)?\]? (?:- )?[^:\n]{1,40}: \S",
    r"^\[\d{1,2}:\d{2}(?::\d{2})?(?:\s?[ap]m)?\] <?(?!(?:info|debug|warn|warning|error|trace|fatal|critical|inf|dbg|wrn|err|vrb|ftl)\b)[\w .'-]{2,30}>?: \S",
)]
STREET = re.compile(r"\b\d{1,6}[ \t]+(?:[NSEW]\.?[ \t]+)?(?:[A-Z][a-z]+[ \t]+){1,3}(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|"
                    r"Drive|Ln|Lane|Way|Ct|Court|Pl|Place|Ter|Terrace|Pkwy|Parkway|Cir|Circle|Hwy|Highway)\b\.?")
PHONE = re.compile(r"(?<![\w.-])(?:\+?1[ .-]?)?\(?[2-9]\d{2}\)?[ .-]\d{3}[ .-]\d{4}(?![\w-])")
# apps whose windows are private by nature: a screenshot of one is a hit before anyone reads it
PRIVATE_APPS = [(re.compile(p, re.I), kind, what) for p, kind, what in (
    (r"\bdiscord\b", "chats", "a Discord window"),
    (r"\bslack\b", "chats", "a Slack window"),
    (r"\bwhatsapp\b", "chats", "a WhatsApp window"),
    (r"\btelegram\b", "chats", "a Telegram window"),
    (r"\bsignal\.exe\b|^Signal$", "chats", "a Signal window"),
    (r"\bmessenger\b", "chats", "a Messenger window"),
    (r"\bms-teams\.exe\b|\| Microsoft Teams\b", "chats", "a Teams chat"),
    (r"\bgmail\b|\b(?:olk|outlook|thunderbird)\.exe\b|\bOutlook$|\bMail$", "chats", "an email window"),
    (r"\b(?:1password|bitwarden|keepass(?:xc)?|lastpass|dashlane|proton pass)\b", "secrets", "a password manager"),
    (r"\bmychart\b|\bpatient portal\b", "health", "a patient portal"),
)]


class GoPrivate(BaseException):
    """The chat turned private while a task was sending to an API model: the task starts over on this PC (boss.run).
    A BaseException, so the retry-the-next-model handlers (except Exception) can't swallow it."""

    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


def private_in(err: BaseException) -> GoPrivate | None:
    """The GoPrivate inside an error, however deep the MCP clients' task groups nested it, else None."""
    if isinstance(err, GoPrivate):
        return err
    if isinstance(err, BaseExceptionGroup):
        for sub in err.exceptions:
            if (found := private_in(sub)) is not None:
                return found
    return None


def digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:20]


def luhn_ok(d: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(d)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def card_brand(d: str) -> str:
    if d[0] == "4" and len(d) in (13, 16, 19):
        return "Visa"
    if len(d) == 16 and (51 <= int(d[:2]) <= 55 or 2221 <= int(d[:4]) <= 2720):
        return "Mastercard"
    if len(d) == 15 and d[:2] in ("34", "37"):
        return "American Express"
    if 16 <= len(d) <= 19 and (d[:4] == "6011" or d[:2] == "65" or 644 <= int(d[:3]) <= 649):
        return "Discover"
    return ""


def iban_ok(s: str) -> bool:
    s = s.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    moved = s[4:] + s[:4]
    try:
        return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1
    except ValueError:
        return False


def pattern_hits(text: str, kinds) -> list[dict]:
    """The pattern scan: what it found, as [{"kind", "what", "keys"}]. keys name each value by its hash (a value you let
    through once is let through wherever it turns up again), never the value itself."""
    out: list[dict] = []
    kinds = set(kinds)

    def hit(kind: str, what: str, value: str) -> None:
        if kind in kinds and not any(h["what"] == what for h in out):
            out.append({"kind": kind, "what": what, "keys": ["value:" + digest(value)]})

    if "secrets" in kinds:
        for rx, what in SECRET_RES:
            for m in rx.finditer(text):
                value = m.group(0)
                if not PLACEHOLDER.search(value.split("=", 1)[-1].split(":", 1)[-1]):
                    hit("secrets", what, value)
                    break
    if "payment" in kinds:
        for m in CARD.finditer(text):
            raw = m.group(0)
            d = re.sub(r"\D", "", raw)
            if not 13 <= len(d) <= 19 or len(set(d)) < 2 or d in TEST_CARDS or not luhn_ok(d):
                continue
            groups = re.split(r"[ -]", raw)
            if len(groups) > 1 and len({len(g) for g in groups[:-1]}) > 2:
                continue  # 12 345 6789 01234: numbers in a table, not one card
            if brand := card_brand(d):
                hit("payment", f"{'an' if brand[0] in 'AEIOU' else 'a'} {brand} card number ending {d[-4:]}", d)
        for m in IBAN.finditer(text):
            if iban_ok(m.group(0)):
                hit("payment", "a bank account number (IBAN)", m.group(0).replace(" ", ""))
    if "ids" in kinds:
        for rx in (SSN_LABELED, SSN):
            if m := rx.search(text):
                hit("ids", "a social security number", re.sub(r"\D", "", m.group(0)))
                break
    if "chats" in kinds:
        lines = sum(len(rx.findall(text)) for rx in CHAT_LINES)
        if lines >= 4:
            app = next((a for a in ("Discord", "WhatsApp", "Slack", "Telegram", "Messenger") if a.lower() in text.lower()), "")
            hit("chats", f"a {app} chat history".replace("a  ", "a ") if app else "a chat history", text[:2000])
    if "contact" in kinds:
        if m := STREET.search(text):
            hit("contact", "a street address", m.group(0))
        phones = PHONE.findall(text)
        if phones:
            hit("contact", "a phone number", phones[0])
    return out


def worth_reading(text: str, kinds) -> bool:
    """Whether the local model should look at a text: it carries a word or shape one of the kinds comes with."""
    if any(SIGNAL_RES[k].search(text) for k in kinds if k in SIGNAL_RES):
        return True
    if "chats" in kinds:
        names = [n.strip().lower() for n in SPEAKER.findall(text) if not NOT_SPEAKER.match(n.strip())]
        counts = Counter(names)
        return len(names) >= 3 and counts.most_common(1)[0][1] >= 2 and len(counts) <= 8
    return False


def mask(what: str) -> str:
    """A model's description of a hit, with anything that looks like the value itself taken out."""
    what = re.sub(r"\d[\d ./-]{4,}\d", "…", str(what))
    what = re.sub(r"\b[A-Za-z0-9_+/=-]{24,}\b", "…", what)
    return what.strip()[:120]


def chunks(text: str) -> list[str]:
    """Up to MAX_CHUNKS pieces of a long text for the local model: its beginning and its end. A document says what it
    is near its top; the pattern scan has already been over all of it."""
    if len(text) <= CHUNK:
        return [text]
    out = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
    return out if len(out) <= MAX_CHUNKS else out[:MAX_CHUNKS - 1] + out[-1:]


def page_url(name: str, args: dict, text: str) -> str:
    url = str(args.get("url") or "") if isinstance(args, dict) else ""
    if not url:
        m = re.search(r"(?:Page URL|URL|Address):\s*(\S+)", text[:2000])
        url = m.group(1) if m else ""
    return url


def public_page(url: str) -> bool:
    host = urllib.parse.urlsplit(url if "://" in url else "https://" + url).hostname or ""
    return bool(host) and not PRIVATE_SITES.search(host) and host not in ("localhost", "127.0.0.1")


def foreground() -> str:
    """The front window's title and program file ("Title | discord.exe"), for screenshots."""
    try:
        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        hwnd = u32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(hwnd, buf, 512)
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = ""
        h = k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            try:
                size = ctypes.c_ulong(1024)
                path = ctypes.create_unicode_buffer(1024)
                if k32.QueryFullProcessImageNameW(h, 0, path, ctypes.byref(size)):
                    exe = Path(path.value).name
            finally:
                k32.CloseHandle(h)
        return f"{buf.value} | {exe}"
    except Exception:
        return ""


def private_window(text: str) -> tuple[str, str, str] | None:
    """(kind, what, app) when a screenshot's window is a private app, by the words that came with it and the front window."""
    for where in (text, foreground()):
        for rx, kind, what in PRIVATE_APPS:
            if where and rx.search(where):
                return kind, what, rx.pattern
    return None


def tool_calls_of(messages) -> dict:
    """tool call id -> (tool name, its arguments), from the assistant turns of a conversation."""
    calls: dict = {}
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                try:
                    a = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    a = {}
                calls[tc.get("id")] = (str(fn.get("name") or ""), a if isinstance(a, dict) else {})
    return calls


def text_of(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(str(p.get("text") or "") for p in c if isinstance(p, dict) and p.get("type") == "text")
    return str(c or "")


WORD = re.compile(r"[A-Za-z][A-Za-z'-]{3,}|\d[\d,./-]{2,}\d")


def read_words(messages) -> set:
    """Names and numbers that came from what a task read on this PC (files, windows, commands, not web pages) and not
    from your own words or IO's instructions: a web search carrying one of them carries something of yours."""
    calls, local, common = tool_calls_of(messages), set(), set()
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role, text = m.get("role"), text_of(m)
        if role == "tool":
            name = calls.get(m.get("tool_call_id"), ("", {}))[0]
            if name in WEB_ARGS or name in PUBLIC_TOOLS or name.startswith("browser_"):
                continue
            local.update(w.lower() for w in WORD.findall(text) if w[0].isupper() or w[0].isdigit())
        elif role in ("system", "user"):
            common.update(w.lower() for w in WORD.findall(text))
    return local - common


_verdicts: dict = {}  # (content hash, kinds) -> hits: each text is screened once per IO run
_allow_lock = threading.Lock()
_allowed: set | None = None


def allowed_hashes() -> set:
    global _allowed
    with _allow_lock:
        if _allowed is None:
            try:
                _allowed = set(json.loads(ALLOW_FILE.read_text(encoding="utf-8")).get("allowed", []))
            except (OSError, ValueError):
                _allowed = set()
        return _allowed


def allow_hashes(hashes) -> None:
    allowed = allowed_hashes()
    with _allow_lock:
        allowed.update(h for h in hashes if h)
        try:
            ALLOW_FILE.parent.mkdir(parents=True, exist_ok=True)
            ALLOW_FILE.write_text(json.dumps({"allowed": sorted(allowed)[-5000:]}), encoding="utf-8")
        except OSError:
            pass


def remember(key: tuple, hits: list) -> None:
    if len(_verdicts) > 4000:
        _verdicts.clear()
    _verdicts[key] = hits


def read_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class Guard:
    """One task's privacy state. Every thread of the task shares it (boss keeps it in a context variable), so a choice
    made in one (send anyway, keep it local) holds for all of them at once.

    check, kinds: Settings > Privacy. private: the chat is private already (no API at all). allowed: what you let
    through in this chat before. ask: the task's question callback (async), loop: the event loop it runs on.
    unattended: nobody is there to answer (go private without asking). local_chat(system, user, max_tokens): one
    question to the local model with thinking off. describe(image part): the local eyes model's words for a picture.
    on_private(why), on_allow(keys): tell the app (they run on the event loop). log(kind, **fields): the task's log."""

    def __init__(self, *, check: bool = True, kinds=None, private: bool = False, allowed=None, ask=None, loop=None,
                 unattended: bool = False, local_chat=None, describe=None, on_private=None, on_allow=None, log=None) -> None:
        self.check = check
        self.kinds = [k for k in (DEFAULT_KINDS if kinds is None else kinds) if k in KINDS]
        self.private, self.why = private, "this chat is private" if private else ""
        self.allowed = set(allowed or [])
        self.ask, self.loop, self.unattended = ask, loop, unattended
        self.local_chat, self.describe = local_chat, describe
        self.on_private, self.on_allow, self.log = on_private, on_allow, log or (lambda *a, **k: None)
        self.user_images: set[str] = set()  # the user's own attached pictures (hashes): described and read, not just named
        self.clean: set[str] = set()  # hashes this task found nothing in
        self.lock = threading.Lock()

    def quiet(self) -> "Guard":
        """A copy for work after the task has answered (writing its playbook): a hit stops that work without asking
        anyone or making the chat private."""
        g = Guard(check=self.check, kinds=self.kinds, private=self.private, allowed=self.allowed, unattended=True,
                  local_chat=self.local_chat, describe=self.describe)  # and nothing in the task's log, which has ended
        g.why, g.user_images, g.clean = self.why, set(self.user_images), set(self.clean)
        return g

    # ---- the gate: nim.create calls it before any request leaves the PC

    def gate(self, model: str, messages) -> None:
        if self.private:
            raise GoPrivate(self.why)
        if not self.check or not self.kinds or not messages:
            return
        with self.lock:  # one screening, and one question, at a time: a racing thread waits here and sees the outcome
            if self.private:
                raise GoPrivate(self.why)
            hits = [h for h in self.scan(messages) if not any(k in self.allowed for k in h["keys"])]
            if hits:
                self.decide(hits, model)

    def scan(self, messages) -> list[dict]:
        calls = tool_calls_of(messages)
        hits: list[dict] = []
        for m in messages:
            if not isinstance(m, dict) or m.get("role") not in ("system", "user", "tool", "assistant"):
                continue
            role, content = m["role"], m.get("content")
            parts = content if isinstance(content, list) else [{"type": "text", "text": str(content or "")}]
            texts = [str(p.get("text") or "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
            name, args = calls.get(m.get("tool_call_id"), ("", {})) if role == "tool" else ("", {})
            for t in texts:
                hits += self.scan_text(t, role, name, args)
            for p in parts:
                if isinstance(p, dict) and p.get("type") == "image_url":
                    hits += self.scan_image(p, " ".join(texts))
        seen, out = set(), []
        for h in hits:
            if (h["what"], h["source"]) not in seen:
                seen.add((h["what"], h["source"]))
                out.append(h)
        return out

    def scan_text(self, text: str, role: str, name: str, args: dict) -> list[dict]:
        if len(text.strip()) < 12:
            return []
        h = digest(text)
        if h in self.clean or h in allowed_hashes():
            return []
        key = (h, tuple(self.kinds), role)
        if key not in _verdicts:
            found = pattern_hits(text, self.kinds)
            # the local model reads what the patterns can't judge (a chat, a contract, a lab result). The brain's own
            # words in this task only repeat what was already screened, and IO's long system prompt is its own
            # instructions, so those get the patterns alone (a short one, like ask_model's with the goal in it, is read)
            readable = role in ("user", "tool") or (role == "system" and len(text) < 4000)
            if not found and readable and len(text) >= MODEL_MIN and self.model_should_read(name, args, text):
                for piece in chunks(text):
                    found = self.model_hits(piece)
                    if found:
                        break
            remember(key, found)
        found = _verdicts[key]
        if not found:
            self.clean.add(h)
            return []
        source, src_key = self.source_of(role, name, args)
        out = []
        for f in found:
            # a value is let through by its own key; anything else by where it came from (this file, your messages)
            keys = list(f.get("keys") or []) + [f"{src_key}:{f['kind']}", "text:" + h]
            out.append({"kind": f["kind"], "what": f["what"], "source": source, "keys": keys, "hash": h})
        return out

    def model_should_read(self, name: str, args: dict, text: str) -> bool:
        if name in PUBLIC_TOOLS or not self.local_chat:
            return False
        path = str(args.get("path") or args.get("file") or "") if isinstance(args, dict) else ""
        if path and Path(path).suffix.lower() in CODE_EXT:
            return False
        if name.startswith("browser_") or name in ("read_page", "Scrape"):
            url = page_url(name, args, text)
            if url and public_page(url):
                return False
        return worth_reading(text, self.kinds)

    def model_hits(self, text: str) -> list[dict]:
        rules = "\n".join(f"- {k}: {KIND_RULES[k]}" for k in self.kinds)
        try:
            reply = self.local_chat(MODEL_SYSTEM.format(rules=rules, keys=", ".join(self.kinds)), text, 300)
        except Exception as e:  # the local model is down or busy: the patterns are all this text gets
            self.log("warning", text=f"privacy check: the local model didn't answer ({type(e).__name__}); patterns only")
            return []
        found = read_json(reply).get("found")
        if not isinstance(found, list):
            return []
        out = []
        for f in found:
            if isinstance(f, dict) and f.get("kind") in self.kinds and str(f.get("what") or "").strip():
                out.append({"kind": f["kind"], "what": mask(f["what"]), "keys": []})
        return out[:3]

    def scan_image(self, part: dict, words: str) -> list[dict]:
        url = str((part.get("image_url") or {}).get("url") or "")
        if not url:
            return []
        h = digest(url)
        if h in self.clean or h in allowed_hashes():
            return []
        if h in self.user_images:  # your own picture: the eyes model describes it, and that is read like any text
            key = (h, tuple(self.kinds), "picture")
            if key not in _verdicts:
                found = []
                if self.describe:
                    try:
                        said = self.describe(part)
                        found = pattern_hits(said, self.kinds) or (self.model_hits(said) if len(said) >= 40 else [])
                    except Exception as e:
                        self.log("warning", text=f"privacy check: couldn't describe a picture ({type(e).__name__})")
                remember(key, found)
            found = _verdicts[key]
            if not found:
                self.clean.add(h)
            return [{"kind": f["kind"], "what": f["what"], "source": "a picture you attached",
                     "keys": list(f.get("keys") or []) + [f"picture:{f['kind']}", "text:" + h], "hash": h} for f in found]
        app = private_window(words)  # a screenshot: private by which app it shows
        if not app or app[0] not in self.kinds:
            self.clean.add(h)
            return []
        kind, what, pattern = app
        return [{"kind": kind, "what": what, "source": "a screenshot", "keys": [f"window:{pattern}:{kind}", "text:" + h], "hash": h}]

    @staticmethod
    def source_of(role: str, name: str, args: dict) -> tuple[str, str]:
        """Where a text came from, in words and as a key."""
        if role in ("user", "system"):
            return "your messages", "chat-message"
        if role == "assistant":
            return "an earlier answer", "chat-answer"
        obj = ""
        if isinstance(args, dict):
            obj = next((str(args[k]) for k in ("path", "file", "url", "window", "title", "name", "app", "query", "command")
                        if isinstance(args.get(k), (str, int)) and str(args.get(k)).strip()), "")
        if obj:
            return f"{obj[:80]}" + ("…" if len(obj) > 80 else ""), f"src:{name}:{obj.lower()[:200]}"
        return f"what {name or 'a tool'} returned", f"src:{name}"

    # ---- the choice

    def decide(self, hits: list[dict], model: str) -> None:
        said = "; ".join(f"{h['what']} in {h['source']}" for h in hits[:3]) + (f" (and {len(hits) - 3} more)" if len(hits) > 3 else "")
        try:
            on_loop = asyncio.get_running_loop() is self.loop
        except RuntimeError:
            on_loop = False
        if self.unattended or self.ask is None or self.loop is None or on_loop:
            # nobody to ask (or no way to wait for an answer here): the safe choice
            self.go_private(f"it read {said}", asked=False)
        who = PROVIDER_NAMES.get(model.split(":")[0], "NVIDIA") if ":" in model else "NVIDIA"
        q = (f"{QUESTION} IO read {said}. Send it to the API model ({who}) anyway, or keep this chat on this PC from now on? "
             "(choices: Keep it local | Send anyway)")
        self.log("privacy", hits=[{"kind": h["kind"], "what": h["what"], "source": h["source"]} for h in hits[:6]], asked=True)
        try:
            answer = asyncio.run_coroutine_threadsafe(self.ask(q), self.loop).result()
        except Exception as e:
            answer = f"(no answer: {e})"
        if re.match(r"\s*(send|yes|allow|ok|go)\b", str(answer), re.I):
            keys = {k for h in hits for k in h["keys"]}
            self.allowed |= keys
            allow_hashes(h.get("hash") for h in hits)  # this exact content may go from now on, in any chat
            if self.on_allow:
                self.loop.call_soon_threadsafe(self.on_allow, sorted(keys))
            self.log("privacy", sent=True, text=f"Sent anyway: {said}")
            return
        self.go_private(f"it read {said}", asked=True)

    def go_private(self, why: str, asked: bool) -> None:
        self.private, self.why = True, why
        self.log("privacy", private=True, text=f"Kept this chat on this PC: {why}", asked=asked)
        if self.on_private and self.loop is not None:
            self.loop.call_soon_threadsafe(self.on_private, why)
        raise GoPrivate(why)

    # ---- Private mode: what may still go to the web

    def query_problem(self, text: str, messages=None) -> str:
        """In a private chat, a web search or page address that carries private details: what it carries ('' if none).
        Names and numbers taken from what the task read here count, whatever they are (the local model can't tell a
        private person's name from anyone else's); then the patterns; then the local model, for the rest."""
        text = str(text or "").strip()
        if not text:
            return ""
        taken = sorted({w.lower() for w in WORD.findall(text)} & read_words(messages))
        if taken:
            return "words from what it read on this PC: " + ", ".join(taken[:4])
        found = pattern_hits(text, KINDS)
        if found:
            return found[0]["what"]
        if not self.local_chat or len(text) < 3:
            return ""
        try:
            reply = self.local_chat(QUERY_SYSTEM, text[:1500], 60)
        except Exception:
            return ""
        m = re.match(r"\s*PRIVATE\s*:?\s*(.*)", reply or "", re.I)
        return mask(m.group(1) or "private details") if m else ""


# web tools in a private chat: what each sends out
WEB_ARGS = {"web_search": "query", "web_answer": "question", "research": "question", "read_page": "url", "browser_open": "url",
            "browser_navigate": "url", "Scrape": "url"}


def outgoing(name: str, args: dict) -> str:
    """The words a web tool would send out (a search, an address), '' for other tools."""
    key = WEB_ARGS.get(name)
    if not key or not isinstance(args, dict):
        return ""
    value = str(args.get(key) or "")
    if key == "url" and value:
        parts = urllib.parse.urlsplit(value if "://" in value else "https://" + value)
        return urllib.parse.unquote_plus(f"{parts.path} {parts.query}").strip() if (parts.query or len(parts.path) > 1) else ""
    return value
