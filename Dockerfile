FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create data directory
RUN mkdir -p data

# Start both the ingestor (background) and the web server (foreground)
CMD ["sh", "-c", "python ingestor/ingestor.py & uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
