FROM python:3.12-slim-bookworm

# qemu-utils: qemu-img / qemu-nbd; cifs-utils + nfs-common: mounting backup targets;
# util-linux: blockdev; udev: udevadm settle before detaching nbd devices
RUN apt-get update \
 && apt-get install -y --no-install-recommends qemu-utils cifs-utils nfs-common util-linux udev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# runs as root on purpose: hot-added block devices, nbd and mounts need it (container is privileged)
EXPOSE 8083
VOLUME ["/var/lib/hcm-proxy"]
CMD ["python", "proxy.py"]
