FROM python:3.12-slim

WORKDIR /app

COPY services/common ./services/common
RUN pip install --no-cache-dir ./services/common

COPY services/mqtt-ingest/requirements.txt ./services/mqtt-ingest/requirements.txt
RUN pip install --no-cache-dir -r services/mqtt-ingest/requirements.txt

COPY services/mqtt-ingest/main.py ./main.py

CMD ["python", "main.py"]
