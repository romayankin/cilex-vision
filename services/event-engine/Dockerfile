FROM python:3.11-slim

WORKDIR /app

COPY services/event-engine/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY proto/ /proto/
COPY services/event-engine/ .

RUN bash gen_proto.sh

EXPOSE 8080

ENTRYPOINT ["python", "main.py"]
