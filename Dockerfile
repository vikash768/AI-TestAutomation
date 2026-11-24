# Runs both the API and the worker. Playwright's own image carries Chromium
# and every system library it needs; the tag must match the pip version.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Tier 2 static-analysis binaries (the pip tools come from requirements.txt):
# gitleaks (secrets) and trivy (dependency / container CVEs across languages).
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; case "$arch" in amd64) gla=x64; tra=64bit;; arm64) gla=arm64; tra=ARM64;; *) gla=x64; tra=64bit;; esac; \
    curl -sSL "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_${gla}.tar.gz" | tar -xz -C /usr/local/bin gitleaks; \
    curl -sSL "https://github.com/aquasecurity/trivy/releases/download/v0.58.1/trivy_0.58.1_Linux-${tra}.tar.gz" | tar -xz -C /usr/local/bin trivy; \
    gitleaks version && trivy --version | head -1

COPY qaplatform/ ./qaplatform/
RUN mkdir -p /app/artifacts

EXPOSE 8000
CMD ["uvicorn", "qaplatform.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
