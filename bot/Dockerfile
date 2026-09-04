FROM python:3.14-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
RUN useradd --create-home --uid 10001 botuser \
    && mkdir -p /data \
    && chown botuser:botuser /data

WORKDIR /app/src
USER botuser
CMD ["python", "bot.py"]
