# Образ spotter-tap-build: armhf gcc с glibc 2.28, программы колонки (glibc 2.30) с ним совместимы.
# Ставит prepare, программы собирает build.
FROM debian:buster

ENV DEBIAN_FRONTEND=noninteractive

RUN printf 'Acquire::Check-Valid-Until "false";\n' > /etc/apt/apt.conf.d/99no-check-valid \
 && printf 'deb http://archive.debian.org/debian buster main\n' > /etc/apt/sources.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gcc-arm-linux-gnueabihf libc6-dev-armhf-cross binutils-arm-linux-gnueabihf \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
