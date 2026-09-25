FROM python:3.12-slim

WORKDIR /app

# Copy application source
COPY app/ /app/app/

# Environment defaults
ENV PYTHONUNBUFFERED=1 \
    PORT=25580 \
    MODS_DIR=/data/mods \
    STATE_DIR=/data/state \
    DIST_ROOT=/data/dist_root

# Prepare mount points
RUN mkdir -p /data/mods /data/state /data/dist_root && \
    chmod -R 777 /data/state /data/mods /data/dist_root

EXPOSE 25580

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:25580/healthz')" || exit 1

CMD ["python", "-m", "app.main"]
