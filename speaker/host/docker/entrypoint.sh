#!/bin/sh
set -e
mkdir -p /storage/yse/bin /storage/yse/app /storage/yse/run/units /storage/yse/run/restart \
  /storage/yse-mww/trainer /storage/yse/models
install -m 755 /opt/yse/docker/yse-ssh /storage/yse/bin/yse-ssh
install -m 755 /opt/yse/docker/ssh-empty /storage/yse/bin/ssh-empty
if ! /storage/yse-mww/trainer/.venv/bin/python -c "import tensorflow, pymicro_features" >/dev/null 2>&1; then
  sh /opt/yse/docker/train-venv.sh >/storage/yse/run/train-venv.log 2>&1 &
fi
if ! docker image inspect spotter-tap-build:latest >/dev/null 2>&1; then
  docker build -t spotter-tap-build:latest -f /storage/yse/pkg/Dockerfile /storage/yse/pkg \
    >/storage/yse/run/spotter-build.log 2>&1 &
fi
exec python3 -u /opt/yse/docker/yse_dock.py
