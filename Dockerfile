# Use Python 3.11 slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY data/ ./data/

# Create uploads directory
RUN mkdir -p uploads

# Expose port (Railway/Render will set $PORT dynamically)
EXPOSE 8000

# Run the application
# Use shell form to allow environment variable substitution
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
