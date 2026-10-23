# Use an official lightweight Python runtime
FROM python:3.11-slim

# Prevent Python from writing .pyc files and force unbuffered stdout/stderr logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

# Set working directory inside the container
WORKDIR /app

# Install minimal OS dependencies needed for compiling C extensions (if any) and curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy backend requirements first to leverage Docker layer caching
# (Only the backend dependencies are needed for the API server)
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy backend application source code
COPY backend/ ./

# Create a non-privileged user and switch to it for security
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Healthcheck to verify the server is responding
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:${PORT}/docs || exit 1

# Expose the default port (documentation only)
EXPOSE 8000

# Start Uvicorn bound dynamically to $PORT (compatible with Render, Railway, Cloud Run)
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT}"]