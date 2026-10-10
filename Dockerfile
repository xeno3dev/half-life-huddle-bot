FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py shift_message.py shifts.sql seed_shifts.py seed_weekend.py ./
RUN useradd --uid 10001 --create-home bot
USER bot
CMD ["python", "bot.py"]
