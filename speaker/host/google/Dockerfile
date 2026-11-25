FROM python:3.11-bookworm
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive DISPLAY=:99
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb x11vnc iproute2 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir playwright \
    && playwright install chromium \
    && playwright install-deps chromium
COPY yse_google_ai.py /app/yse_google_ai.py
CMD ["python", "-u", "/app/yse_google_ai.py"]
