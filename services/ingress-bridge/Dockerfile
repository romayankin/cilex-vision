# Ingress Bridge — NATS JetStream to Kafka bridge with local spool
# Build from repo root: docker build -f services/ingress-bridge/Dockerfile .
FROM python:3.11-slim

WORKDIR /app

COPY services/ingress-bridge/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY proto/ /proto/
COPY services/ingress-bridge/ .
RUN bash gen_proto.sh

EXPOSE 8080 9091
CMD ["python", "main.py"]
