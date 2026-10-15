# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Runtime is pure standard library, so no dependency installation is required.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    HOST=0.0.0.0 \
    DATA_DIR=/data

RUN groupadd --system --gid 1001 watchdog \
    && useradd --system --uid 1001 --gid watchdog --home-dir /app --no-create-home watchdog

WORKDIR /app
COPY apiwatch ./apiwatch

RUN mkdir -p /data && chown -R watchdog:watchdog /app /data

USER watchdog
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "apiwatch"]
