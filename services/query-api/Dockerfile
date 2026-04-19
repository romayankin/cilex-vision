FROM python:3.11-slim

# ffmpeg for /clips/range segment concat (Phase 9)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY services/query-api/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy service code
COPY services/query-api/ .

EXPOSE 8000

ENTRYPOINT ["python", "main.py"]
