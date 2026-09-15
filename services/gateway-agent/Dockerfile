FROM python:3.12-slim

WORKDIR /app

COPY services/common ./services/common
RUN pip install --no-cache-dir ./services/common

COPY services/gateway-agent/requirements.txt ./services/gateway-agent/requirements.txt
RUN pip install --no-cache-dir -r services/gateway-agent/requirements.txt

COPY services/gateway-agent/main.py ./main.py

CMD ["python", "main.py"]
