FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -r -u 998 hcmbackup
USER hcmbackup

EXPOSE 8082

# single process on purpose: APScheduler lives in it, a second worker would fire backups twice
CMD ["python", "backup.py"]
