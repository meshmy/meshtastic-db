FROM python:3.12-slim

WORKDIR /app

COPY services/common ./services/common
RUN pip install --no-cache-dir ./services/common

COPY services/tcp-poller/main.py ./main.py

CMD ["python", "main.py"]
