FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy everything the bot needs. This used to name each file, which meant a
# new data file (seed_events.json) was silently left out of the image and the
# bot started with an empty calendar. .dockerignore decides what stays out.
COPY . .

CMD ["python", "eiac_bible_bot_groq.py"]
