FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    curl && \
    rm -rf /var/lib/apt/lists/*

# Copy project metadata files
COPY pyproject.toml README.md LICENSE ./

# Copy source code
COPY src/ ./src/

# Copy config directory - IMPORTANT!
COPY config/ ./config/

# Create logs directory
RUN mkdir -p logs

# Install dependencies
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -e .

# Verify config file exists (for debugging)
RUN ls -la config/ && cat config/tool_packages.yaml | head -10

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Start server
CMD ["sh", "-c", "servicenow-mcp-sse --host=0.0.0.0 --port=${PORT:-8080}"]
