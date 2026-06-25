"""
EIAC Bible Study Bot — Telegram
================================
A bilingual (English + Farsi/Persian) Bible-study companion for
Emmanuel Iranian Anglican Church (EIAC).

Powered by Groq + Llama 3.3 70B (free). Uses long-polling, so it works
anywhere you can run Python — no public server or webhook needed.

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
import sys

from groq import Groq
from telegram import Update
from telegram.constants import ChatAction
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

# Llama 3.3 70B on Groq — free, fast, multilingual (knows English + Farsi well).
MODEL = "llama-3.3-70b-versatile"

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
an Anglican church serving the Iranian / Persian community. You help people
explore the Bible with warmth, depth, and pastoral care.

LANGUAGE — this is very important:
- Detect the language of the user's message.
- If the user writes in Persian / Farsi, reply ENTIRELY in natural, fluent Farsi.
- If the user writes in English, reply ENTIRELY in English.
- If they mix languages, follow the dominant language of their latest message.
- Never switch languages unless the user does.

WHEN SOMEONE ASKS ABOUT A VERSE, PHRASE, KEYWORD, OR TOPIC:
- Identify the relevant Bible verse, even from a partial phrase or a theme.
- Give the verse IN CONTEXT: quote the verse just BEFORE it, the verse ITSELF,
  and the verse just AFTER it, each clearly labelled with its reference
  (e.g. John 3:15 / John 3:16 / John 3:17).
- Then briefly explain: (1) the historical background / setting, (2) the meaning,
  and (3) a short, encouraging reflection for daily life.
- Keep quotations accurate. If you are unsure of the exact wording of a verse,
  say so honestly rather than inventing text.

TONE & BOUNDARIES:
- Warm, pastoral, humble, and non-judgmental — in the Anglican tradition.
- You may share Christian perspective and encouragement, but never pressure,
  shame, or condemn anyone.
- For serious personal crises (e.g. self-harm, abuse, deep despair), gently
  encourage the person to reach out to a pastor, a trusted person, or local
  emergency / support services, alongside any spiritual encouragement.
- Keep answers focused and readable. Use short paragraphs. Plain text only
  (this is a Telegram chat) — no markdown symbols like ** or ##.
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


def ask_groq(chat_id: int, user_text: str) -> str:
    """Send the conversation (system + history + new message) to Groq."""
    history = conversations.setdefault(chat_id, [])
    history.append({"role": "user", "content": user_text})
    _trim_history(chat_id)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + conversations[chat_id]

    completion = groq_client.chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=0.6,
        max_tokens=1024,
    )
    reply = completion.choices[0].message.content.strip()

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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_text = update.message.text

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        reply = ask_groq(chat_id, user_text)
        await update.message.reply_text(reply)
    except Exception:  # noqa: BLE001
        logger.exception("Groq call failed while handling a message")
        await update.message.reply_text(_ERROR_REPLY)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

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

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("new", new_conversation))
    app.add_handler(CommandHandler("verse", verse))
    app.add_handler(CommandHandler("about", about))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("EIAC Bible Bot is starting (polling)... Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
