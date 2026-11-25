FROM python:3.12-slim
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
RUN pip install "faster-whisper==1.1.1" "nvidia-cublas-cu12==12.4.5.8" "nvidia-cudnn-cu12==9.1.0.70" requests
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib
COPY stt_server.py /app/stt_server.py
ENV STT_ENGINE=whisper
EXPOSE 8000
CMD ["python", "/app/stt_server.py"]
