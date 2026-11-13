FROM python:3.11-slim
WORKDIR /app

# install system dependencies (curl for vendor assets, git + docker compose v2 for auto-update)
# the compose binary is fetched for the image's own CPU architecture so Apply Update works on
# arm64 hosts (Apple Silicon laptops, Ampere VMs) as well as x86_64
RUN apt-get update && apt-get install -y curl git docker.io && rm -rf /var/lib/apt/lists/* && \
    ARCH="$(uname -m)" && \
    curl -fsSL "https://github.com/docker/compose/releases/download/v2.27.1/docker-compose-linux-${ARCH}" \
      -o /usr/local/bin/docker-compose && \
    chmod +x /usr/local/bin/docker-compose && \
    mkdir -p /usr/local/lib/docker/cli-plugins && \
    ln -s /usr/local/bin/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose && \
    docker-compose version && \
    git config --global --add safe.directory /repo

COPY dashboard/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY dashboard/app.py .
COPY dashboard/config_db.py .
COPY dashboard/user_db.py .
COPY dashboard/versioning.py .
COPY dashboard/forwarding.py .
COPY dashboard/alerts.py .
COPY dashboard/storage_summary.py .
COPY dashboard/manual.py .
COPY dashboard/cluster_registry.py .
COPY dashboard/hypercore_tasks.py .
COPY dashboard/hcm_crypto.py .
COPY dashboard/migrations.py .
COPY dashboard/reports/ reports/
COPY dashboard/static/ static/
COPY dashboard/templates/ templates/
# the user manual and the guides it links, served by the dashboard at /manual
COPY docs/manual/ manual/
COPY docs/administration.md manual/extra/

# Download external dependencies for offline/dark-site support
RUN mkdir -p static/vendor && \
    curl -sL "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js" -o static/vendor/chart.umd.min.js && \
    curl -sL "https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js" -o static/vendor/chartjs-adapter-date-fns.bundle.min.js && \
    curl -sL "https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&family=Space+Grotesk:wght@300;400;500;600;700&display=swap" -o static/vendor/fonts.css && \
    # Download the actual font files referenced in the CSS
    grep -oP 'url\(\K[^)]+' static/vendor/fonts.css | while read url; do \
        FILENAME=$(echo "$url" | sed 's/.*\///;s/?.*//' ); \
        curl -sL "$url" -o "static/vendor/${FILENAME}"; \
        sed -i "s|${url}|/static/vendor/${FILENAME}|g" static/vendor/fonts.css; \
    done && \
    # noVNC (VM console): the release's ES modules (core/) and its vendored pako, MPL-2.0
    curl -sL "https://github.com/novnc/noVNC/archive/refs/tags/v1.5.0.tar.gz" | tar -xz -C /tmp && \
    mkdir -p static/vendor/novnc && \
    cp -r /tmp/noVNC-1.5.0/core /tmp/noVNC-1.5.0/vendor /tmp/noVNC-1.5.0/LICENSE.txt static/vendor/novnc/ && \
    rm -rf /tmp/noVNC-1.5.0

EXPOSE 5000
# threaded workers: a VM console is a long-lived websocket (/console/ws) that holds one thread for
# its duration; sync workers would be blocked by it and killed by their request timeout
CMD ["gunicorn", "-k", "gthread", "-w", "4", "--threads", "16", "-b", "0.0.0.0:5000", "app:app"]