FROM python:3.12-slim-bookworm

# qemu-utils: qemu-img / qemu-nbd; cifs-utils + nfs-common: mounting backup targets;
# util-linux: blockdev/sfdisk/losetup; udev: udevadm settle; lvm2 + dmsetup: file-level recovery
# (COW overlays, LVM import); ntfs-3g: NTFS fallback; genisoimage: ISO delivery; procps: ps/free for
# the support bundle; tini: PID 1 (see below)
RUN apt-get update \
 && apt-get install -y --no-install-recommends qemu-utils cifs-utils nfs-common util-linux fdisk udev lvm2 dmsetup cryptsetup-bin ntfs-3g genisoimage openssl procps tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# runs as root on purpose: hot-added block devices, nbd and mounts need it (container is privileged)
EXPOSE 8083
VOLUME ["/var/lib/hcm-proxy"]
# tini is PID 1 and reaps orphans. `qemu-nbd -c` and ntfs-3g daemonise, so their servers are re-parented
# to PID 1; with python as PID 1 nobody waits for them, so every disconnect leaves a zombie. The likely
# cause of proxy 0.5.4 failing to create any thread after ~1000 jobs (four days of hourly backups); the
# support bundle's processes.txt and limits.txt are there to confirm it. In the image, not
# in `docker run --init`, so proxies that are already deployed get it with the next update.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "proxy.py"]
