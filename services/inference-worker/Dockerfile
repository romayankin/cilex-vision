FROM python:3.11-slim

WORKDIR /app

# Install system deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY services/inference-worker/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy proto definitions (needed for gen_proto.sh)
COPY proto/ /proto/

# Copy service code
COPY services/inference-worker/ .

# Generate protobuf Python code
RUN bash gen_proto.sh

EXPOSE 9090

ENTRYPOINT ["python", "main.py"]
