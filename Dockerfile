FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=UTF-8

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy only files that exist
COPY pyproject.toml ./
COPY src/ ./src/

# Copy README and LICENSE if they exist, otherwise skip
COPY README.md ./ 2>/dev/null || echo "No README.md"
COPY LICENSE ./ 2>/dev/null || echo "No LICENSE"

# Create and copy config directory
RUN mkdir -p ./config
COPY config/*.yaml ./config/ 2>/dev/null || echo "No config files"

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

RUN mkdir -p /app/logs

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD servicenow-mcp-sse --host=0.0.0.0 --port=${PORT:-8080}
