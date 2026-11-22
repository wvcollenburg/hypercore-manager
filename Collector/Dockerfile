# the Debian release is named: package names (apt) differ between releases, and a rebuild must not
# move to the next one unasked
FROM python:3.11-slim-trixie
WORKDIR /app
COPY requirements.txt constraints.txt ./
# constraints.txt: every transitive package at the version this release was tested with
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt
COPY collector.py .
COPY config_db.py .
COPY cluster_registry.py .
COPY hypercore_tasks.py .
COPY hypercore_session.py .
COPY hcm_crypto.py .
CMD ["python", "-u", "collector.py"]