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

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY config_db.py .
COPY user_db.py .
COPY versioning.py .
COPY storage_summary.py .
COPY legacy_metrics.py .
COPY cluster_registry.py .
COPY hypercore_tasks.py .
COPY hcm_crypto.py .
COPY migrations.py .
COPY reports/ reports/
COPY static/ static/
COPY templates/ templates/

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
    done

EXPOSE 5000
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:app"]