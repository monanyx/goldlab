# goldbot for a small VPS (the only place live mode is meant to run).
#   docker build -t goldbot .
#   docker run -d --name goldbot --restart unless-stopped --env-file goldbot.env \
#     -v $(pwd)/state:/app/state goldbot
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=UTC
WORKDIR /app

COPY pyproject.toml README.md ./
COPY goldlab ./goldlab
COPY goldbot ./goldbot
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 goldbot \
    && mkdir -p /app/state && chown goldbot /app/state
COPY goldbot.toml ./

USER goldbot
VOLUME ["/app/state"]
CMD ["python", "-m", "goldbot", "--config", "goldbot.toml", "loop", "--interval", "60"]
