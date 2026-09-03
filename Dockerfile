# Production Dockerfile for Headless Renewal Automation with Playwright
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

WORKDIR /app

# Install system utilities and cron
RUN apt-get update && apt-get install -y --no-install-recommends \
    cron \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browser dependencies (Chromium)
RUN playwright install chromium

# Copy application source code
COPY . .

# Create persistent data directories
RUN mkdir -p data/input_reports data/downloads data/credentials

# Set Python path and environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV PLAYWRIGHT_HEADLESS=true

# Default entrypoint: Run the daily renewal cycle or daemon
CMD ["python", "src/main.py", "--daemon"]
