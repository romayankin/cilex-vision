FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/recorder-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/recorder-service/*.py ./

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
