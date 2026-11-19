# Base image with Python
FROM python:3.12-slim

WORKDIR /app

# Install dependencies and the Chromium browser with its system libraries
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

# Copy the rest of the project
COPY . .

# Start the watcher
CMD ["python", "watcher.py"]
