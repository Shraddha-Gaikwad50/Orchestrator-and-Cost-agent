FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY infra/cloudrun/sync-online-monitor-firestore/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY scripts/sync-online-monitor-to-firestore.py /app/scripts/sync-online-monitor-to-firestore.py

ENTRYPOINT ["python", "/app/scripts/sync-online-monitor-to-firestore.py"]
