# Headless Chromium for `make screenshots` (the n8n workflow and the API docs).
# Microsoft's Playwright image ships the browsers but not the Python package, whose
# version must match them: upgrade the image tag and requirements.in together, then run
# `make lock`. Digest checked against mcr.microsoft.com.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble@sha256:330d401ae23c905896a0c26c66c36fc971b621519e7b11b435a8328d7bf6e031

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Optional PyPI mirror for restricted networks; hash pinning means a mirror cannot
# substitute different files. The image's Python is Ubuntu's, hence
# --break-system-packages (nothing else in this image uses it).
ARG PIP_INDEX_URL=""
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --break-system-packages \
        ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY capture.py /app/capture.py
ENTRYPOINT ["python3", "/app/capture.py"]
