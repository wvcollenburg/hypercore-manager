FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -r -u 999 sentinel
USER sentinel

EXPOSE 8080

CMD ["python", "sentinel.py"]
