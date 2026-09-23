"""
EIAC Bible Study Bot — Telegram
================================
A bilingual (English + Farsi/Persian) Bible-study companion for
Emmanuel Iranian Anglican Church (EIAC).

Powered by Groq (free tier). Uses long-polling, so it works anywhere you
can run Python — no public server or webhook needed.

Setup
-----
1. Install dependencies:
       pip install -r requirements.txt
2. Provide your keys as environment variables (never hard-code them):
       Windows (PowerShell):
           setx TELEGRAM_TOKEN "your-telegram-token"
           setx GROQ_API_KEY   "your-groq-key"
       Then open a NEW terminal so the variables take effect.
3. Run:
       python eiac_bible_bot_groq.py

The bot keeps running as long as this script runs. Stop it with Ctrl+C.
"""

import logging
import os
import re
import sys
import json
import threading
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
from groq import Groq, RateLimitError
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

# Groq retires models with little notice — qwen3-32b and then
# llama-3.3-70b-versatile were both pulled out from under this bot. So the
# model is a list tried in order, and a retired one is skipped automatically
# instead of taking the whole bot down.
#
# qwen3.8-27b leads: of what Groq hosts today it writes the most natural
# Farsi, holds a warm pastoral register, and answers in plain prose. The
# gpt-oss models reply in Markdown tables, which are unreadable in a
# plain-text Telegram message. The old Qwen <think>-tag problem is gone in
# this generation.
MODELS = [
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
]
_active_model = MODELS[0]

# Optional: a Telegram chat id that gets told when something breaks. Without
# it the bot still self-heals, it just does so silently. Set ADMIN_CHAT_ID in
# the environment to turn alerts on.
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")

# Groq's free tier caps OUTPUT tokens per minute (1000 at the time of
# writing). Asking for more than that in a single request is rejected
# outright with a 429, so the ceiling has to sit below the limit - not at
# it. 800 is still a long, unhurried answer.
MAX_OUTPUT_TOKENS = 800

# How often the watchdog re-checks that Groq still serves our model.
HEALTH_CHECK_SECONDS = 600

# The church is in Melbourne; "today" must mean today there, not wherever
# the server happens to be.
CHURCH_TZ = "Australia/Melbourne"

# Church events live on a Railway volume mounted at /data so they survive a
# redeploy. Falls back to the working directory when running on a laptop.
EVENTS_PATH = Path(os.environ.get("EVENTS_PATH", "/data/events.json"))
if not EVENTS_PATH.parent.exists():
    EVENTS_PATH = Path(__file__).parent / "events.json"

# Who may add or remove events. Comma-separated Telegram chat ids. Everyone
# else can read the list but not change it.
EVENT_ADMINS = {
    chat_id.strip()
    for chat_id in os.environ.get("EVENT_ADMIN_IDS", "").split(",")
    if chat_id.strip()
}

# How many past messages (user + assistant combined) to keep per user.
# Keeps context useful while staying well within the model's token budget.
MAX_HISTORY_MESSAGES = 20

logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)
# python-telegram-bot's HTTP layer is chatty; quiet it down.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("eiac-bible-bot")


# --------------------------------------------------------------------------- #
# The bot's "personality" — a single source of truth for how it should answer.
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """\
You are the Bible Study companion for Emmanuel Iranian Anglican Church (EIAC),
an Anglican church serving the Iranian and Persian community. You help people
explore the Bible with warmth, depth, and pastoral care.

═══════════════════════════════════════════════
SCRIPT & LANGUAGE RULES — follow these exactly
═══════════════════════════════════════════════
1. NEVER output Chinese, Japanese, Korean, or any East-Asian characters under
   any circumstances. Not even one character. This is a strict rule.
2. Only two languages are allowed in your replies: Persian/Farsi and English.
3. Detect the user's language by looking at their script and words:
   - If the message contains Persian/Arabic script letters (ا ب پ ت ث ج چ ...)
     or common Farsi words, reply ENTIRELY in natural, fluent Farsi (Persian).
   - If the message is in Latin/English script, reply ENTIRELY in English.
   - A single word in Persian script = reply in Farsi.
   - A single word in English = reply in English.
   - Never mix the two languages in a single reply.
   - Never switch language unless the very next user message uses the other language.
4. When replying in Farsi, write EVERY name in Persian script — people, places
   and books of the Bible alike (Nicodemus -> نیقودیموس, Corinthians -> قرنتیان).
   Never leave Latin letters inside a Farsi word. If it helps the reader, the
   English spelling may follow in brackets, but the Persian form comes first.

═══════════════════════════════════════════════
HANDLING A SINGLE WORD OR VERY SHORT MESSAGE
═══════════════════════════════════════════════
When the user sends only one, two, or three words (e.g. "محبت", "hope", "ایمان",
"forgiveness"), treat it as a Bible keyword search. Do the following:

Step 1 — Acknowledge the word and briefly explain it means in biblical context
         (one sentence, in the user's language).
Step 2 — List 3 to 5 key Bible references where this word or theme appears,
         each with a very short (one-line) description. Number them.
         Example format (in Farsi):
           این کلمه در جاهای مختلفی در کتاب‌مقدس آمده است، از جمله:
           1. یوحنا ۳:۱۶ — خدا جهان را آنقدر محبت کرد که...
           2. اول قرنتیان ۱۳:۴ — محبت شکیباست، محبت مهربان است...
           3. رومیان ۸:۳۸-۳۹ — هیچ‌چیز نمی‌تواند ما را از محبت خدا جدا کند...
Step 3 — Ask which one they would like to explore further. Keep the question warm
         and inviting (one sentence).

═══════════════════════════════════════════════
HANDLING A FULL VERSE, PHRASE, OR CLEAR TOPIC
═══════════════════════════════════════════════
When the user's message is a clear phrase, partial verse, or question:
- Identify the most relevant Bible verse.
- Give the verse IN CONTEXT: quote the verse BEFORE it, the verse ITSELF, and
  the verse AFTER it — each labelled with its reference (e.g. John 3:15 / 3:16 / 3:17).
- Then explain: (1) historical background/setting, (2) the meaning, and
  (3) a short encouraging reflection for daily life.
- Quote verses accurately. If unsure of the exact wording, say so honestly.

═══════════════════════════════════════════════
TONE & BOUNDARIES
═══════════════════════════════════════════════
- Warm, pastoral, humble, non-judgmental — Anglican tradition.
- Encourage but never pressure, shame, or condemn.
- For serious crises (self-harm, abuse, deep despair): gently suggest speaking
  to a pastor, trusted person, or local support services.
- Short paragraphs. Plain text only — NO markdown (* # _ `), NO bullet symbols
  from other scripts, NO emojis unless the user uses them first.
"""

WELCOME_EN = (
    "✝️ Welcome to the EIAC Bible Study Bot!\n\n"
    "I'm here to help you explore God's Word — in English or Persian (فارسی).\n\n"
    "You can:\n"
    "• Ask about a verse, even a partial phrase ("
    "\"For God so loved the world\")\n"
    "• Ask about a topic (hope, forgiveness, fear)\n"
    "• Ask what a passage means\n\n"
    "Commands:\n"
    "/verse — a verse to encourage you\n"
    "/new — start a fresh conversation\n"
    "/help — how to use me\n"
    "/about — about EIAC\n\n"
    "Just send me a message to begin. 🙏"
)

WELCOME_FA = (
    "✝️ به ربات مطالعهٔ کتاب‌مقدس کلیسای امانوئل خوش آمدید!\n\n"
    "من اینجا هستم تا به شما کمک کنم کلام خدا را کشف کنید — به فارسی یا انگلیسی.\n\n"
    "شما می‌توانید:\n"
    "• دربارهٔ یک آیه بپرسید، حتی با بخشی از آن\n"
    "• دربارهٔ یک موضوع بپرسید (امید، بخشش، ترس)\n"
    "• معنای یک قسمت را بپرسید\n\n"
    "دستورها:\n"
    "/verse — یک آیه برای دلگرمی شما\n"
    "/new — شروع گفت‌وگوی تازه\n"
    "/help — راهنمای استفاده\n"
    "/about — دربارهٔ کلیسا\n\n"
    "برای شروع، کافی است پیامی بفرستید. 🙏"
)

# --------------------------------------------------------------------------- #
# Church events
#
# A small JSON list kept on a persistent volume. Events drop off the list by
# themselves the day after they happen, so nobody has to remember to tidy up.
# --------------------------------------------------------------------------- #

SEED_PATH = Path(__file__).parent / "seed_events.json"


def seed_events() -> None:
    """Merge any events shipped in the repo into the stored list.

    This is the second way events get in: the office adds them from a phone
    with /addevent, and a batch of announcements can also arrive by deploy.

    Matching is on title plus date, so redeploying never duplicates anything,
    and an event added from a phone is never touched or removed by a deploy.
    """
    if not SEED_PATH.exists():
        return

    try:
        seed = json.loads(SEED_PATH.read_text(encoding="utf8"))
    except Exception:  # noqa: BLE001 — a bad seed file must not stop the bot
        logger.exception("Could not read %s", SEED_PATH.name)
        return

    stored = load_events()
    known = {(e.get("title"), e.get("date")) for e in stored}
    fresh = [e for e in seed if (e.get("title"), e.get("date")) not in known]

    if fresh:
        save_events(stored + fresh)
        logger.info("Seeded %s event(s) from %s", len(fresh), SEED_PATH.name)


def today_at_church() -> date:
    """Today's date in Melbourne, whatever timezone the server thinks it is in."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(CHURCH_TZ)).date()
    except Exception:  # noqa: BLE001 — missing tzdata must not break the bot
        logger.warning("Timezone data unavailable; falling back to server date.")
        return date.today()


def load_events() -> list[dict]:
    """Every stored event, including ones that have already happened."""
    try:
        return json.loads(EVENTS_PATH.read_text(encoding="utf8"))
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001 — a corrupt file must not take the bot down
        logger.exception("Could not read %s", EVENTS_PATH)
        return []


def save_events(events: list[dict]) -> None:
    EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVENTS_PATH.write_text(
        json.dumps(events, ensure_ascii=False, indent=2), encoding="utf8"
    )


def _runs_indefinitely(event: dict) -> bool:
    """True for something with no end in sight - a class that just runs.

    Written as "until": "open". It stays on the calendar until someone
    removes it with /delevent, which is the point: nobody knows yet when
    the last session will be.
    """
    until = event.get("until")
    return isinstance(until, str) and until.strip().lower() == "open"

def upcoming_events() -> list[dict]:
    """Events still to come, soonest first.

    A one-off stays listed all through its own day. Something that runs for a
    while - a term of classes, a window for handing in forms - carries an
    "until" date and stays listed until that day passes, so it does not vanish
    after its first session.
    """
    today = today_at_church()
    live = []
    for event in load_events():
        try:
            when = date.fromisoformat(event["date"])
        except (KeyError, ValueError):
            continue
        if _runs_indefinitely(event):
            live.append(event)
            continue
        try:
            last_day = date.fromisoformat(event["until"])
        except (KeyError, ValueError, TypeError):
            last_day = when
        if max(when, last_day) >= today:
            live.append(event)
    return sorted(live, key=lambda e: e["date"])


def parse_event_date(text: str) -> str | None:
    """Accept 2026-12-25 or Australian 25/12/2026. Returns ISO form, or None."""
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def describe_event(event: dict, index: int | None = None) -> str:
    """One event rendered as a few readable lines."""
    when = date.fromisoformat(event["date"])
    head = when.strftime("%A %d %B %Y")
    if event.get("time"):
        head += f", {event['time']}"

    if _runs_indefinitely(event):
        head = f"ongoing, from {when.strftime('%A %d %B %Y')}"
        if event.get("time"):
            head += f" — {event['time']}"
    elif event.get("until"):
        try:
            ends = date.fromisoformat(event["until"])
            if ends > when:
                started = "started " if when < today_at_church() else "from "
                head = f"{started}{when.strftime('%A %d %B')}, until {ends.strftime('%d %B %Y')}"
                if event.get("time"):
                    head += f" — {event['time']}"
        except ValueError:
            pass

    label = f"{index}. " if index is not None else ""
    lines = [f"{label}{event['title']} — {head}"]
    if event.get("location"):
        lines.append(f"   Where: {event['location']}")
    if event.get("responsible"):
        lines.append(f"   Run by: {event['responsible']}")
    if event.get("contact"):
        lines.append(f"   Contact: {event['contact']}")
    if event.get("notes"):
        lines.append(f"   Note: {event['notes']}")
    return "\n".join(lines)


def events_for_prompt() -> str:
    """The event list as the model should see it, appended to the system prompt.

    Rebuilt on every question rather than cached, so an event added a minute
    ago is answerable immediately and a finished one disappears on its own.
    """
    events = upcoming_events()
    today = today_at_church().strftime("%A %d %B %Y")

    if not events:
        return (
            "\n\n=== CHURCH EVENTS ===\n"
            f"Today is {today}. There are NO events currently scheduled.\n"
            "If someone asks about events, say plainly that nothing is on the "
            "calendar right now and suggest they ask at church. Never invent an "
            "event, a date, a name or a phone number."
        )

    listed = "\n".join(describe_event(e, i) for i, e in enumerate(events, 1))
    return (
        "\n\n=== CHURCH EVENTS ===\n"
        f"Today is {today}. These are the upcoming events at EIAC:\n\n"
        f"{listed}\n\n"
        "When someone asks about an event — when it is, where, who is running "
        "it, who to contact — answer ONLY from this list, in their language. "
        "Translate the details into Farsi when replying in Farsi, but keep "
        "names and phone numbers exactly as written. Never invent an event, a "
        "date, a name or a phone number. If they ask about something not on "
        "the list, say it is not on the calendar."
    )


# --------------------------------------------------------------------------- #
# State & Groq client
# --------------------------------------------------------------------------- #

# Per-user conversation memory: { chat_id: [ {role, content}, ... ] }
# In-memory only — resets if the script restarts. That's fine for a study bot.
conversations: dict[int, list[dict[str, str]]] = {}

groq_client: Groq | None = None  # initialised in main()


def _trim_history(chat_id: int) -> None:
    """Keep only the most recent messages so prompts stay small."""
    history = conversations.get(chat_id, [])
    if len(history) > MAX_HISTORY_MESSAGES:
        conversations[chat_id] = history[-MAX_HISTORY_MESSAGES:]


class BusyError(Exception):
    """Every model was rate limited. Worth telling the person to retry."""


def _model_retired(exc: Exception) -> bool:
    """True when Groq rejected the call because the model no longer exists."""
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("model_not_found", "does not exist", "decommission", "deprecat")
    )


def notify_admin(message: str) -> None:
    """Tell the admin something broke. Silent no-op if ADMIN_CHAT_ID is unset."""
    if not ADMIN_CHAT_ID:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": ADMIN_CHAT_ID, "text": f"EIAC Bible Bot: {message}"},
            timeout=10,
        )
    except Exception:  # noqa: BLE001 — an alert failing must never stop the bot
        logger.exception("Could not reach the admin chat")


def _watchdog() -> None:
    """Re-check Groq on a timer and re-point at a live model if ours vanishes.

    This is the failure that has taken this bot down twice: Groq drops a model,
    every reply starts failing, and nobody finds out until a person complains.
    Checking on a timer means the bot repairs itself in the quiet hours instead
    of in front of the congregation.
    """
    consecutive_failures = 0

    while True:
        time.sleep(HEALTH_CHECK_SECONDS)
        previous = _active_model

        if pick_model():
            consecutive_failures = 0
            if _active_model != previous:
                logger.warning("Watchdog: %s -> %s", previous, _active_model)
                notify_admin(
                    f"Groq retired {previous}. Switched to {_active_model} on "
                    f"my own — no action needed, but the model list is worth "
                    f"a look."
                )
            continue

        consecutive_failures += 1
        logger.warning("Watchdog: Groq check failed (%s in a row)", consecutive_failures)
        # ~30 minutes, then ~2 hours. Enough to ignore a blip, soon enough to act.
        if consecutive_failures in (3, 12):
            notify_admin(
                f"Groq has been unreachable for {consecutive_failures} checks "
                f"(~{consecutive_failures * HEALTH_CHECK_SECONDS // 60} minutes). "
                f"People may be getting error replies."
            )


def pick_model() -> bool:
    """Ask Groq which of our models it still serves, and settle on the best one.

    Done at startup, and again every HEALTH_CHECK_SECONDS by the watchdog,
    so a retirement costs a single request here rather than a failed call
    (and its retries) on the first person who says hello.

    Returns True if we are pointed at a model Groq will actually serve.
    """
    global _active_model
    try:
        live = {m.id for m in groq_client.models.list().data}
    except Exception:  # noqa: BLE001 — a probe failure must not stop the bot
        logger.warning("Could not list Groq models; falling back at call time.")
        return False

    for model in MODELS:
        if model in live:
            if model != MODELS[0]:
                logger.warning(
                    "%s is no longer served by Groq — using %s instead.",
                    MODELS[0], model,
                )
            _active_model = model
            return True

    logger.error(
        "Groq serves none of %s. Pick a replacement from this list and update "
        "MODELS: %s", MODELS, ", ".join(sorted(live)),
    )
    notify_admin(
        "Groq no longer serves any model this bot knows about. It cannot "
        "answer anyone until MODELS is updated."
    )
    return False


def _complete(messages: list[dict[str, str]]) -> str:
    """Ask Groq, stepping past any model it has retired since the last run."""
    global _active_model

    order = [_active_model] + [m for m in MODELS if m != _active_model]
    last_error: Exception | None = None

    for model in order:
        try:
            # A retired model returns 404, which the SDK would otherwise retry
            # with a long backoff before we ever get to the next candidate.
            completion = groq_client.with_options(max_retries=1).chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.6,
                max_tokens=MAX_OUTPUT_TOKENS,
            )
        except RateLimitError as exc:
            # Each model has its own budget, so a neighbour may have room.
            logger.warning("%s is rate limited — trying the next model", model)
            last_error = exc
            continue
        except Exception as exc:  # noqa: BLE001 — re-raised unless it is a dead model
            if not _model_retired(exc):
                raise
            logger.warning("Groq no longer serves %s — trying the next model", model)
            last_error = exc
            continue

        if model != _active_model:
            logger.warning("Model switched: %s -> %s", _active_model, model)
            _active_model = model
        # Reasoning models can return None content alongside a `reasoning` field.
        return (completion.choices[0].message.content or "").strip()

    if isinstance(last_error, RateLimitError):
        raise BusyError() from last_error

    raise RuntimeError(
        "Groq is serving none of the models in MODELS — the list needs updating. "
        f"Last error: {last_error}"
    )


def ask_groq(chat_id: int, user_text: str) -> str:
    """Send the conversation (system + history + new message) to Groq."""
    history = conversations.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_text})
    _trim_history(chat_id)

    # Rebuilt per question so a just-added event is answerable at once.
    system = SYSTEM_PROMPT + events_for_prompt()
    messages = [{"role": "system", "content": system}] + conversations[chat_id]

    reply = _complete(messages)
    # Strip Qwen thinking blocks — handle both closed and unclosed tags
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL)
    reply = re.sub(r"<think>.*", "", reply, flags=re.DOTALL).strip()

    history.append({"role": "assistant", "content": reply})
    _trim_history(chat_id)
    return reply


# --------------------------------------------------------------------------- #
# Command handlers
# --------------------------------------------------------------------------- #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME_EN)
    await update.message.reply_text(WELCOME_FA)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "How to use me / راهنما\n\n"
        "Just type your question in English or Persian. Examples:\n"
        "• \"For God so loved the world\"\n"
        "• \"What does Psalm 23 mean?\"\n"
        "• \"verses about hope\"\n"
        "• \"خدا محبت است\"\n\n"
        "Commands:\n"
        "/start — welcome message\n"
        "/events — church events coming up / برنامه‌های کلیسا\n"
        "/verse — an encouraging verse\n"
        "/new — clear our conversation and start fresh\n"
        "/about — about EIAC\n"
        "/help — this message"
    )
    await update.message.reply_text(text)


async def new_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conversations.pop(update.effective_chat.id, None)
    await update.message.reply_text(
        "✨ Started a fresh conversation. What would you like to explore?\n"
        "✨ گفت‌وگوی تازه‌ای آغاز شد. دوست دارید چه چیزی را بررسی کنیم؟"
    )


async def verse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask the model for an encouraging verse, in the user's recent language."""
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        reply = ask_groq(
            chat_id,
            "Please share one encouraging Bible verse for today, with its "
            "reference, plus the verse before and after it for context, and a "
            "short one- or two-sentence reflection. Respond in the same language "
            "I have been using with you (default to English if unsure).",
        )
        await update.message.reply_text(reply)
    except BusyError:
        await update.message.reply_text(_BUSY_REPLY)
    except Exception:  # noqa: BLE001 — surface a friendly message, log the detail
        logger.exception("Groq call failed in /verse")
        await update.message.reply_text(_ERROR_REPLY)


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "About EIAC / دربارهٔ کلیسا\n\n"
        "Emmanuel Iranian Anglican Church (EIAC) is an Anglican church serving "
        "the Iranian and Persian community. This bot is a free Bible-study "
        "companion to help you read and reflect on Scripture in English and "
        "Persian.\n\n"
        "کلیسای انگلیکن ایرانی امانوئل (EIAC) خدمتگزار جامعهٔ ایرانی و فارسی‌زبان "
        "است. این ربات همراهی رایگان برای مطالعهٔ کتاب‌مقدس به انگلیسی و فارسی است.\n\n"
        "May the Lord bless you. 🙏  خداوند شما را برکت دهد."
    )
    await update.message.reply_text(text)


_ADD_EVENT_HELP = (
    "To add an event, send it on one line with | between the parts:\n\n"
    "/addevent Title | date | time | place | who runs it | contact\n\n"
    "Example:\n"
    "/addevent Christmas Service | 25/12/2026 | 6:00 PM | Church Hall | "
    "Pastor John | 0400 123 456\n\n"
    "Only the title and the date are required — leave the rest out if you "
    "do not know them yet:\n"
    "/addevent Working Bee | 11/10/2026\n\n"
    "Dates can be 25/12/2026 or 2026-12-25.\n"
    "The event disappears by itself the day after it happens."
)


def _is_event_admin(chat_id: int) -> bool:
    return str(chat_id) in EVENT_ADMINS


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tell a person their own Telegram id, so they can be made an admin."""
    await update.message.reply_text(
        f"Your Telegram id is: {update.effective_chat.id}\n"
        f"شناسه تلگرام شما: {update.effective_chat.id}"
    )


async def events_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show what is coming up. Anyone may ask."""
    events = upcoming_events()

    if not events:
        await update.message.reply_text(
            "There are no events on the calendar at the moment.\n"
            "در حال حاضر هیچ برنامه‌ای در تقویم نیست."
        )
        return

    listed = "\n\n".join(describe_event(e, i) for i, e in enumerate(events, 1))
    await update.message.reply_text(
        f"Upcoming at EIAC / برنامه‌های پیشِ رو\n\n{listed}\n\n"
        f"Ask me about any of these in English or Farsi."
    )


async def add_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add an event. Admins only."""
    chat_id = update.effective_chat.id

    if not EVENT_ADMINS:
        await update.message.reply_text(
            "No event administrator has been set up yet, so I cannot add "
            f"events.\n\nYour Telegram id is {chat_id} — give it to whoever "
            "looks after the bot and they can set EVENT_ADMIN_IDS."
        )
        return

    if not _is_event_admin(chat_id):
        await update.message.reply_text(
            "Only the church office can add events, but you can see them all "
            "with /events.\nفقط دفتر کلیسا می‌تواند برنامه اضافه کند."
        )
        return

    raw = " ".join(context.args).strip()
    if not raw:
        await update.message.reply_text(_ADD_EVENT_HELP)
        return

    parts = [part.strip() for part in raw.split("|")]
    title = parts[0]
    if not title:
        await update.message.reply_text(_ADD_EVENT_HELP)
        return

    if len(parts) < 2 or not parts[1]:
        await update.message.reply_text(
            "I need a date for that event.\n\n" + _ADD_EVENT_HELP
        )
        return

    when = parse_event_date(parts[1])
    if not when:
        await update.message.reply_text(
            f"I could not read \"{parts[1]}\" as a date.\n"
            f"Try 25/12/2026 or 2026-12-25."
        )
        return

    if date.fromisoformat(when) < today_at_church():
        await update.message.reply_text(
            "That date has already passed, so nobody would ever see it. "
            "Check the year?"
        )
        return

    def field(i: int) -> str:
        return parts[i].strip() if len(parts) > i else ""

    event = {
        "title": title,
        "date": when,
        "time": field(2),
        "location": field(3),
        "responsible": field(4),
        "contact": field(5),
        "notes": field(6),
        "added_by": str(chat_id),
    }

    events = load_events()
    events.append(event)
    save_events(events)
    logger.info("Event added: %s on %s", title, when)

    await update.message.reply_text(
        "Added. People can now ask me about it.\n\n" + describe_event(event)
    )


async def delete_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove an event by its number in /events. Admins only."""
    chat_id = update.effective_chat.id

    if not _is_event_admin(chat_id):
        await update.message.reply_text("Only the church office can remove events.")
        return

    events = upcoming_events()
    if not events:
        await update.message.reply_text("There is nothing on the calendar to remove.")
        return

    if not context.args:
        listed = "\n\n".join(describe_event(e, i) for i, e in enumerate(events, 1))
        await update.message.reply_text(
            f"Which one? Send /delevent and its number.\n\n{listed}"
        )
        return

    try:
        index = int(context.args[0])
        doomed = events[index - 1]
        if index < 1:
            raise IndexError
    except (ValueError, IndexError):
        await update.message.reply_text(
            f"Pick a number between 1 and {len(events)} — /events shows them."
        )
        return

    remaining = [
        e
        for e in load_events()
        if not (e.get("title") == doomed["title"] and e.get("date") == doomed["date"])
    ]
    save_events(remaining)
    logger.info("Event removed: %s on %s", doomed["title"], doomed["date"])

    await update.message.reply_text("Removed:\n\n" + describe_event(doomed))


# --------------------------------------------------------------------------- #
# Message handler
# --------------------------------------------------------------------------- #

_BUSY_REPLY = (
    "A lot of people are asking me at once. Please ask again in a minute.\n"
    "تعداد پرسش‌ها زیاد است. لطفاً یک دقیقه دیگر دوباره بپرسید."
)


_ERROR_REPLY = (
    "I'm sorry — I had trouble answering just now. Please try again in a moment.\n"
    "متأسفم — در پاسخ‌دادن مشکلی پیش آمد. لطفاً لحظه‌ای بعد دوباره تلاش کنید."
)


def _is_farsi(text: str) -> bool:
    """Return True if the text contains Persian/Arabic script characters."""
    return any("؀" <= ch <= "ۿ" or "ݐ" <= ch <= "ݿ" for ch in text)


def _build_prompt(user_text: str) -> str:
    words = user_text.strip().split()
    lang = "Farsi/Persian" if _is_farsi(user_text) else "English"
    script = "Persian/Farsi" if _is_farsi(user_text) else "English"

    if len(words) <= 3:
        return (
            f"The user sent a very short message: '{user_text}'\n"
            f"IMPORTANT: Reply ONLY in {lang}. Do NOT use Chinese, Japanese, "
            f"Korean, or any other script. Use ONLY {script} script.\n"
            f"FIRST decide which kind of question this is:\n"
            f"(a) If it is about CHURCH EVENTS — a service, a class, a program, "
            f"a date, who runs something, who to contact — answer from the "
            f"CHURCH EVENTS list in your system prompt. Do NOT treat it as a "
            f"Bible keyword and do NOT offer Bible verses instead.\n"
            f"(b) Otherwise treat it as a Bible keyword or topic search, and "
            f"follow the SINGLE WORD handling instructions in your system "
            f"prompt: acknowledge the word, list 3-5 Bible references with "
            f"one-line descriptions, then ask which one they want to explore."
        )
    return user_text


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_text = update.message.text.strip()

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        prompt = _build_prompt(user_text)
        reply = ask_groq(chat_id, prompt)
        # Safety net: if the reply somehow contains Chinese/CJK characters, ask again
        if any("一" <= ch <= "鿿" for ch in reply):
            lang = "Farsi/Persian" if _is_farsi(user_text) else "English"
            logger.warning("CJK characters detected in reply — retrying with stricter prompt")
            retry_prompt = (
                f"Your previous reply contained Chinese characters which is wrong. "
                f"The user asked about: '{user_text}'. "
                f"Reply ONLY in {lang}. Do not use any Chinese, Japanese, or Korean "
                f"characters whatsoever. Follow the single-word handling instructions."
            )
            reply = ask_groq(chat_id, retry_prompt)
        await update.message.reply_text(reply)
    except BusyError:
        await update.message.reply_text(_BUSY_REPLY)
    except Exception:  # noqa: BLE001
        logger.exception("Groq call failed while handling a message")
        await update.message.reply_text(_ERROR_REPLY)


# --------------------------------------------------------------------------- #
# Health-check server (keeps Render's free web-service tier from sleeping)
# --------------------------------------------------------------------------- #

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 — required method name
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"EIAC Bible Bot is running")

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — silence access logs
        pass


def _start_health_server() -> None:
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health-check server listening on port %s", port)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch anything the handlers did not, so one bad update cannot end the run."""
    error = context.error

    if isinstance(error, Conflict):
        # Two containers overlap for a few seconds on every deploy. Normal.
        logger.warning("Another instance is polling this token; backing off.")
        return

    logger.error("Unhandled error: %s", error, exc_info=error)


def main() -> None:
    global groq_client

    missing = [
        name
        for name, value in (("TELEGRAM_TOKEN", TELEGRAM_TOKEN), ("GROQ_API_KEY", GROQ_API_KEY))
        if not value
    ]
    if missing:
        print(
            "ERROR: missing environment variable(s): " + ", ".join(missing) + "\n\n"
            "Set them and run again. In PowerShell:\n"
            '    setx TELEGRAM_TOKEN "your-telegram-token"\n'
            '    setx GROQ_API_KEY   "your-groq-key"\n'
            "then open a NEW terminal and run:  python eiac_bible_bot_groq.py",
            file=sys.stderr,
        )
        sys.exit(1)

    groq_client = Groq(api_key=GROQ_API_KEY)
    pick_model()
    seed_events()

    # Render's free tier only keeps "web services" alive, so we expose a tiny
    # health-check endpoint on a background thread. An external pinger hitting
    # this URL every few minutes keeps the container (and the bot) awake 24/7.
    _start_health_server()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("new", new_conversation))
    app.add_handler(CommandHandler("verse", verse))
    app.add_handler(CommandHandler("about", about))
    app.add_handler(CommandHandler("events", events_command))
    app.add_handler(CommandHandler("addevent", add_event))
    app.add_handler(CommandHandler("delevent", delete_event))
    app.add_handler(CommandHandler("myid", my_id))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(on_error)

    threading.Thread(target=_watchdog, daemon=True).start()
    logger.info("Watchdog running: Groq re-checked every %ss", HEALTH_CHECK_SECONDS)
    logger.info(
        "Events file: %s (%s upcoming) | admins: %s",
        EVENTS_PATH, len(upcoming_events()), len(EVENT_ADMINS) or "none set",
    )

    logger.info("Model: %s (fallbacks: %s)", _active_model,
                ", ".join(m for m in MODELS if m != _active_model))
    logger.info("EIAC Bible Bot is starting (polling)... Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
