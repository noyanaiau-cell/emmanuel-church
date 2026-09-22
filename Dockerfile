FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY eiac_bible_bot_groq.py .
CMD ["python", "eiac_bible_bot_groq.py"]
