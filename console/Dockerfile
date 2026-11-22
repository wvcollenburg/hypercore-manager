# the Debian release is named: package names (apt) differ between releases, and a rebuild must not
# move to the next one unasked
FROM python:3.12-slim-trixie

# tesseract reads login prompts and lock-screen texts for the open console session check
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt constraints.txt ./
# constraints.txt: every transitive package at the version this release was tested with
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt
COPY . .

RUN useradd -r -u 997 hcmconsole
USER hcmconsole

EXPOSE 8084

# one asyncio process: the console relay and the scheduled open-session check share the HyperCore session
CMD ["python", "main.py"]
