FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates openssl adb openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/lite-kit
COPY lite-kit/ /opt/lite-kit/
COPY lock/ /opt/yse-lock/
COPY panel/app.py /opt/yse-panel/app.py
RUN chmod 755 /opt/yse-lock/askpass.sh /opt/yse-lock/bin/dropbear /opt/yse-lock/bin/dropbearkey \
    && mkdir -p /opt/lite-kit/firmware /opt/lite-kit/log /opt/lite-kit/run

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/yse-lock:/opt/lite-kit/scripts \
    STAND_TZ=Europe/Moscow \
    TZ=Europe/Moscow \
    BUILTIN_DNS=1 \
    DNS_SINK_ALL=1 \
    LOCK_RUN=/opt/lite-kit/run \
    LOCK_LOG=/opt/lite-kit/log \
    LOCK_SKIP=

EXPOSE 53/tcp 53/udp 80/tcp 443/tcp 8090/tcp

CMD ["python3", "/opt/yse-lock/entrypoint.py"]
