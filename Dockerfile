# Runs both the API and the worker. Playwright's own image carries Chromium
# and every system library it needs; the tag must match the pip version.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY qaplatform/ ./qaplatform/
RUN mkdir -p /app/artifacts

EXPOSE 8000
CMD ["uvicorn", "qaplatform.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
