FROM python:3.12-slim

WORKDIR /app

COPY services/common ./services/common
RUN pip install --no-cache-dir ./services/common

COPY services/ingest-api/requirements.txt ./services/ingest-api/requirements.txt
RUN pip install --no-cache-dir -r services/ingest-api/requirements.txt

COPY services/ingest-api/main.py ./main.py

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
