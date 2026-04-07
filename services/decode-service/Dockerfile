FROM python:3.11-slim

WORKDIR /app

# Install GStreamer system packages for H.264/H.265 decode
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        python3-gi \
        gir1.2-gstreamer-1.0 \
        gir1.2-gst-plugins-base-1.0 \
        gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-ugly \
        gstreamer1.0-libav \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY services/decode-service/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy proto definitions (needed for gen_proto.sh)
COPY proto/ /proto/

# Copy service code
COPY services/decode-service/ .

# Generate protobuf Python code
RUN bash gen_proto.sh

EXPOSE 9090

ENTRYPOINT ["python", "main.py"]
