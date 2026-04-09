FROM python:3.11-slim

WORKDIR /app

# Install system deps (OpenCV needs libgl)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY services/attribute-service/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy proto definitions (needed for gen_proto.sh)
COPY proto/ /proto/

# Copy service code
COPY services/attribute-service/ .

# Generate protobuf Python code
RUN bash gen_proto.sh

EXPOSE 8080

ENTRYPOINT ["python", "main.py"]
