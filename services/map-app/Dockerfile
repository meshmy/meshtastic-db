FROM python:3.12-slim

WORKDIR /app

COPY services/common ./services/common
RUN pip install --no-cache-dir ./services/common

COPY services/map-app/requirements.txt ./services/map-app/requirements.txt
RUN pip install --no-cache-dir -r services/map-app/requirements.txt

COPY services/map-app/main.py ./main.py
COPY services/map-app/static ./static

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
