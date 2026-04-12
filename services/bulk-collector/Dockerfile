FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY services/bulk-collector/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY proto /proto
COPY services/bulk-collector /app

RUN bash /app/gen_proto.sh

EXPOSE 8080 9091

CMD ["python", "main.py"]
