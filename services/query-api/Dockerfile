FROM python:3.11-slim

WORKDIR /app

# Install Python deps
COPY services/query-api/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy service code
COPY services/query-api/ .

EXPOSE 8000

ENTRYPOINT ["python", "main.py"]
