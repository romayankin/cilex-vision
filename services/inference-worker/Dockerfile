FROM python:3.11-slim

WORKDIR /app

# Install system deps (git is required for pip install git+https://...)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libglib2.0-0 \
        libgl1 \
        git \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY services/inference-worker/requirements.txt requirements.txt
# Keep torch CPU-only (avoid CUDA wheels) to fit CPU-only deployments
ENV TORCH_CUDA_ARCH_LIST=""
ENV USE_CUDA=0
RUN pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r requirements.txt && \
    pip install --no-cache-dir Cython>=3.0.0 numpy tensorboard && \
    pip install --no-cache-dir --no-build-isolation \
        git+https://github.com/KaiyangZhou/deep-person-reid.git

# Copy proto definitions (needed for gen_proto.sh)
COPY proto/ /proto/

# Copy service code
COPY services/inference-worker/ .

# Generate protobuf Python code
RUN bash gen_proto.sh

EXPOSE 9090

ENTRYPOINT ["python", "main.py"]
