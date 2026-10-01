# ──────────────────────────────────────────────────────────────────────────
# prisma-monitor — FastAPI + aiokafka (monitor en vivo del workflow)  →  :3020
# Sirve la API de métricas y su propio front estático (web/).
#
# Base slim (glibc) y no Alpine: aiokafka/asyncpg traen wheels manylinux y no hace falta compilar.
# ──────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=3020

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY monitor ./monitor
COPY web ./web
COPY scripts ./scripts

RUN useradd --system --uid 10001 app && chown -R app /app
USER app

EXPOSE 3020

HEALTHCHECK --interval=10s --timeout=4s --start-period=10s --retries=6 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:%s/health' % os.environ.get('PORT','3020'), timeout=3)"

CMD ["sh", "-c", "uvicorn monitor.main:create_app --factory --host 0.0.0.0 --port ${PORT}"]
