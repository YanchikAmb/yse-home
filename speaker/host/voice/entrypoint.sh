#!/bin/sh
# Первый запуск собирает окружение голоса в томе и кладёт голоса из репозитория.
set -e
mkdir -p /voice/app /voice/models /voice/rvc /voice/cache/tmp
for name in jarvis vihrov; do
  src=/opt/yse/models/voice/$name/$name.pth
  dst=/voice/rvc/$name/$name.pth
  if [ -s "$src" ] && [ ! -s "$dst" ]; then
    mkdir -p "/voice/rvc/$name"
    cp "$src" "$dst"
  fi
done
if [ ! -s /voice/models/v5_6_ru.pt ]; then
  curl -fsSL -o /voice/models/v5_6_ru.pt https://models.silero.ai/models/tts/ru/v5_6_ru.pt
fi
if [ ! -s /voice/models/voxceleb_resnet34_LM.onnx ]; then
  curl -fsSL -o /voice/models/voxceleb_resnet34_LM.onnx \
    https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/main/voxceleb_resnet34_LM.onnx
fi
if ! /voice/venv/bin/python -c "import torch" >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends build-essential git curl
  python -m venv /voice/venv
  /voice/venv/bin/pip install -q -U "pip<24.1" wheel setuptools==69.5.1
  /voice/venv/bin/pip install -q -r /voice/app/requirements.txt
fi
exec /voice/venv/bin/python -u /voice/app/voice_server.py
