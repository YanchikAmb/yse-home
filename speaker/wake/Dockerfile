FROM python:3.11-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip espeak-ng \
 && pip install --no-cache-dir vosk==0.3.45 \
 && curl -fsSL -o /tmp/model.zip https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip \
 && unzip -q /tmp/model.zip -d /opt \
 && rm /tmp/model.zip \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server.py /app/server.py

RUN python -u /app/server.py --self-test

EXPOSE 18181/udp
CMD ["python", "-u", "/app/server.py"]
