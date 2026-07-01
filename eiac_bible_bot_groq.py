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
import re
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

# Qwen 3.6 27B on Groq — free tier, supports 201 languages including Persian/Farsi.
# Replaces qwen/qwen3-32b which was deprecated by Groq on 2026-06-17.
MODEL = "qwen/qwen3.6-27b"

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
/no_think
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
        extra_body={"thinking": {"type": "disabled"}},
    )
    reply = completion.choices[0].message.content.strip()
    # Strip any Qwen 3 <think>...</think> blocks that may appear in the output
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()

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
    """
    Wrap short single-word queries with an explicit language instruction so the
    model never defaults to Chinese or mixes scripts.
    """
    words = user_text.strip().split()
    lang = "Farsi/Persian" if _is_farsi(user_text) else "English"

    if len(words) <= 3:
        # Single keyword — guide the model explicitly
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
