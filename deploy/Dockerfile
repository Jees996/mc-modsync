FROM python:3.12-slim

WORKDIR /app

# Create non-root user matching host UID/GID 1000:1001
RUN groupadd -g 1001 mcgroup && \
    useradd -u 1000 -g mcgroup -m appuser

# Copy application source
COPY app/ /app/app/

# Environment defaults
ENV PYTHONUNBUFFERED=1 \
    PORT=25580 \
    MODS_DIR=/data/mods \
    STATE_DIR=/data/state

# Prepare mount points
RUN mkdir -p /data/mods /data/state && \
    chown -R appuser:mcgroup /data/state /data/mods /app

USER appuser

EXPOSE 25580

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:25580/healthz')" || exit 1

CMD ["python", "-m", "app.main"]
