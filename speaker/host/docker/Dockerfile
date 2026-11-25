FROM python:3.11-bookworm
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        adb ca-certificates curl docker.io git build-essential \
        gcc-aarch64-linux-gnu libc6-dev-arm64-cross \
        iproute2 openssh-client sudo unzip \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir \
    "wyoming==1.5.4" "websockets>=12" "numpy<3" "onnxruntime>=1.17" \
    "yandex-music==3.2.2" vosk
COPY entrypoint.sh yse_dock.py train-venv.sh yse-ssh ssh-empty /opt/yse/docker/
RUN chmod 755 /opt/yse/docker/entrypoint.sh /opt/yse/docker/train-venv.sh /opt/yse/docker/yse-ssh /opt/yse/docker/ssh-empty \
    && cp /opt/yse/docker/entrypoint.sh /usr/local/bin/yse-entrypoint
ENTRYPOINT ["/usr/local/bin/yse-entrypoint"]
