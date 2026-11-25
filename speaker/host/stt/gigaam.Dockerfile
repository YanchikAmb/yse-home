FROM python:3.12-slim
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg git \
    && rm -rf /var/lib/apt/lists/*
RUN pip install torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cu124
RUN pip install "gigaam @ git+https://github.com/salute-developers/GigaAM.git"
COPY stt_server.py /app/stt_server.py
ENV STT_ENGINE=gigaam
EXPOSE 8000
CMD ["python", "/app/stt_server.py"]
