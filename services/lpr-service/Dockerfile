FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY services/lpr-service/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY proto/ /proto/
COPY services/lpr-service/ .

RUN bash gen_proto.sh

EXPOSE 8080

ENTRYPOINT ["python", "main.py"]
