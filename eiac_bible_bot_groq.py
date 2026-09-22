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
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from groq import Groq
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

# How often the watchdog re-checks that Groq still serves our model.
HEALTH_CHECK_SECONDS = 600

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
                max_tokens=1024,
            )
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

    raise RuntimeError(
        "Groq is serving none of the models in MODELS — the list needs updating. "
        f"Last error: {last_error}"
    )


def ask_groq(chat_id: int, user_text: str) -> str:
    """Send the conversation (system + history + new message) to Groq."""
    history = conversations.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_text})
    _trim_history(chat_id)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + conversations[chat_id]

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


# --------------------------------------------------------------------------- #
# Message handler
# --------------------------------------------------------------------------- #

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

    if len(words) <= 3:
        return (
            f"The user sent a very short message: '{user_text}'\n"
            f"This appears to be a Bible keyword or topic search.\n"
            f"IMPORTANT: Reply ONLY in {lang}. Do NOT use Chinese, Japanese, "
            f"Korean, or any other script. Use ONLY {'Persian/Farsi' if _is_farsi(user_text) else 'English'} script.\n"
            f"Follow the SINGLE WORD handling instructions in your system prompt: "
            f"acknowledge the word, list 3-5 Bible references with one-line descriptions, "
            f"then ask which one the user wants to explore."
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(on_error)

    threading.Thread(target=_watchdog, daemon=True).start()
    logger.info("Watchdog running: Groq re-checked every %ss", HEALTH_CHECK_SECONDS)

    logger.info("Model: %s (fallbacks: %s)", _active_model,
                ", ".join(m for m in MODELS if m != _active_model))
    logger.info("EIAC Bible Bot is starting (polling)... Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
