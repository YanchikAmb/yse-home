#!/bin/sh
# Окружение слова и обучения. Идёт в фоне, диалог от него не ждёт.
set -e
T=/storage/yse-mww/trainer
MODEL=/opt/yse/models/wake/ru_jarvis_mww.tflite
if [ ! -e "$T/train_mww.py" ]; then
  rm -rf "$T"
  git clone -q https://github.com/interkelstar/microwakeword-trainer.git "$T"
fi
if [ -s "$MODEL" ] && [ ! -s "$T/ru_jarvis_mww.tflite" ]; then
  cp "$MODEL" "$T/ru_jarvis_mww.tflite"
fi
if [ ! -x "$T/.venv/bin/python" ]; then
  python3 -m venv "$T/.venv"
fi
if ! "$T/.venv/bin/python" -c "import tensorflow, pymicro_features" >/dev/null 2>&1; then
  echo "TensorFlow и microWakeWord: 10-20 минут"
  "$T/.venv/bin/pip" install -q -U pip setuptools wheel
  "$T/.venv/bin/pip" install -q \
    "microwakeword @ git+https://github.com/kahrendt/microWakeWord.git@a70bd740d4e79ee8a8bb3db843fe862b88d5d6b0" \
    pymicro-features==2.0.2 pyyaml scipy tqdm audiomentations datasets
fi
echo TRAIN-VENV-OK
