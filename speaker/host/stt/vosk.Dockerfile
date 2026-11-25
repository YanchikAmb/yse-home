FROM python:3.12-slim
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1
RUN pip install "vosk==0.3.45" "numpy<3"
COPY stt_server.py /app/stt_server.py
ENV STT_ENGINE=vosk
EXPOSE 8000
CMD ["python", "/app/stt_server.py"]
