# the Debian release is named: package names (apt) differ between releases, and a rebuild must not
# move to the next one unasked
FROM python:3.11-slim-trixie

WORKDIR /app

# Install build dependencies required by Prophet
RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*

COPY requirements.txt constraints.txt ./
# constraints.txt: every transitive package at the version this release was tested with
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt

# Copy your scripts into the container
COPY HyperCore_balancer.py .
COPY predictive_engine.py .
COPY config_db.py .
COPY cluster_registry.py .
COPY hypercore_tasks.py .
COPY hypercore_session.py .
COPY hcm_crypto.py .

# Run the balancer unbuffered so logs show up immediately in Docker
CMD ["python", "-u", "HyperCore_balancer.py"]