# Minimal image for the studyweb HTTP API (the Tavily-replacement server).
FROM python:3.12-slim

# lxml needs libxml2/libxslt at runtime; slim images ship them via pip wheels,
# but build tools help on platforms without prebuilt wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml requirements.txt README.md ./
COPY studyweb ./studyweb
RUN pip install --no-cache-dir .

# Run as an unprivileged user; give it a writable cache dir.
RUN useradd --create-home --uid 10001 studyweb \
    && mkdir -p /var/cache/studyweb \
    && chown -R studyweb /var/cache/studyweb
USER studyweb

EXPOSE 8787
ENV STUDYWEB_CACHE_DIR=/var/cache/studyweb

# Liveness check hits the public /health endpoint (no auth required).
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=4).status==200 else 1)"

# Bind to all interfaces inside the container.
CMD ["python", "-m", "studyweb", "serve", "--host", "0.0.0.0", "--port", "8787"]
