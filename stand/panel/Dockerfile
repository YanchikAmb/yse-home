FROM python:3.12-slim-bookworm
WORKDIR /app
COPY app.py /app/app.py
ENV STAND_RUN=/stand/run \
    STAND_FIRMWARE=/stand/firmware \
    STAND_LOG=/stand/log \
    PANEL_PORT=8090
EXPOSE 8090
CMD ["python3", "/app/app.py"]