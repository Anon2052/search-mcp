# Production image for search-mcp.
# Base image pinned (python 3.12.14, Debian bookworm slim) per build-qa policy.
FROM python:3.12.14-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Python deps first for better layer caching (requirements.txt is pip-tools pinned).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Install the package itself. README.md is required: pyproject.toml references it.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Run as non-root.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health').read()"

# Shell form so the host-injected $PORT expands at runtime.
CMD ["sh", "-c", "uvicorn search_mcp.rest_app:app --host 0.0.0.0 --port ${PORT:-8000}"]
