FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=UTF-8

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

# Create config directory and copy if exists
RUN mkdir -p ./config
COPY config/ ./config/ 2>/dev/null || true

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

RUN mkdir -p /app/logs

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Render provides PORT env var
CMD servicenow-mcp-sse --host=0.0.0.0 --port=${PORT:-8080}
