# Edge Agent — GStreamer + Python RTSP capture, motion filter, NATS publisher
# Build from repo root:  docker build -f services/edge-agent/Dockerfile .
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        pkg-config \
        libgirepository1.0-dev \
        libcairo2-dev \
        gir1.2-gstreamer-1.0 \
        gir1.2-gst-plugins-base-1.0 \
        gstreamer1.0-tools \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-ugly \
        gstreamer1.0-libav \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/edge-agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY proto/ /proto/
COPY services/edge-agent/ .
RUN bash gen_proto.sh

EXPOSE 9090
CMD ["python", "main.py"]
