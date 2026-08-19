FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# One process. The ingestor runs as a scheduled job inside the app (see
# backend/app.py) rather than as a background `&` process — a backgrounded
# ingestor could die without the platform noticing, which is how the archive
# went stale for two months without a single alert.
CMD ["sh", "-c", "uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
