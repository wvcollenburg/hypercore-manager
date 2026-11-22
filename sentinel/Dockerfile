# the Debian release is named: package names (apt) differ between releases, and a rebuild must not
# move to the next one unasked
FROM python:3.12-slim-trixie

WORKDIR /app

COPY requirements.txt constraints.txt ./
# constraints.txt: every transitive package at the version this release was tested with
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt

COPY . .

RUN useradd -r -u 999 sentinel
USER sentinel

EXPOSE 8080

CMD ["python", "sentinel.py"]
