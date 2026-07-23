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

EXPOSE 8787
ENV STUDYWEB_CACHE_DIR=/tmp/studyweb-cache

# Bind to all interfaces inside the container.
CMD ["python", "-m", "studyweb", "serve", "--host", "0.0.0.0", "--port", "8787"]
