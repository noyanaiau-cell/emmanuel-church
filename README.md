# EIAC Bible Study Bot

Bilingual (English + Farsi) Bible-study Telegram bot for Emmanuel Iranian Anglican Church.
Powered by Groq + Llama 3.3 70B — completely free.

## Files

| File | Purpose |
|------|---------|
| `eiac_bible_bot_groq.py` | Telegram bot |
| `eiac-bible-website.html` | Website chatbot (drag-and-drop to Netlify) |
| `CONFIGURE-BOT.html` | Setup helper — generates copy-paste commands |
| `requirements.txt` | Python dependencies |
| `Procfile` | Railway worker definition |

## Quick start (Windows)

1. **Rotate your keys first** (the originals were shared in plain text):
   - Telegram: message @BotFather → `/revoke`
   - Groq: [console.groq.com](https://console.groq.com) → API Keys → Create

2. Open `CONFIGURE-BOT.html` in your browser, enter your keys, and copy the generated commands.

3. Paste the `setx` commands in PowerShell, then **open a new PowerShell window** and run:
   ```
   cd "C:\Users\noyan\OneDrive\Desktop\claude code\eiac-bible-bot"
   "C:\Users\noyan\AppData\Local\Programs\Python\Python312\python.exe" -m pip install -r requirements.txt
   "C:\Users\noyan\AppData\Local\Programs\Python\Python312\python.exe" eiac_bible_bot_groq.py
   ```

4. Message your bot on Telegram to test:
   - English: `For God so loved the world`
   - Farsi: `خدا محبت است`

## Deploy 24/7 on Railway

1. Push this folder to a GitHub repo.
2. Sign in to [railway.app](https://railway.app) → New Project → Deploy from GitHub.
3. Add environment variables in the Railway dashboard:
   - `TELEGRAM_TOKEN` = your telegram token
   - `GROQ_API_KEY` = your groq key
4. Railway picks up `Procfile` and starts the bot as a worker automatically.
5. Check **Logs** — you should see `EIAC Bible Bot is starting (polling)...`

## Deploy website on Netlify

Drag and drop `eiac-bible-website.html` onto [netlify.com](https://netlify.com).
Users enter their own free Groq key when they first open the page.
