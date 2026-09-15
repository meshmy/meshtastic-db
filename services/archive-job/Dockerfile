FROM python:3.12-slim

WORKDIR /app

COPY services/common ./services/common
RUN pip install --no-cache-dir ./services/common

COPY services/archive-job/requirements.txt ./services/archive-job/requirements.txt
RUN pip install --no-cache-dir -r services/archive-job/requirements.txt

COPY services/archive-job/export_parquet.py ./export_parquet.py

CMD ["python", "export_parquet.py"]
