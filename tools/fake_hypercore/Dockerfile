# synthetic HyperCore cluster for local testing (see fake_hypercore.py); never part of the stack
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir aiohttp==3.12.15 pillow==13.0.0 cryptography==42.0.8
WORKDIR /app
COPY fake_hypercore.py .
EXPOSE 443
CMD ["python", "fake_hypercore.py"]
