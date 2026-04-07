FROM python:3.11-slim

# OpenCV headless runtime deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download YOLOv8n weights at build time
RUN python3 -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"

COPY demo.py .

EXPOSE 5000

CMD ["python3", "demo.py"]
