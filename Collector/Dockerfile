FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY collector.py .
COPY config_db.py .
COPY cluster_registry.py .
COPY hypercore_tasks.py .
COPY hcm_crypto.py .
CMD ["python", "-u", "collector.py"]