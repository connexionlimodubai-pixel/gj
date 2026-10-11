FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OPENBERRY_HOME=/data \
    OPENBERRY_DB=/data/openberry.db

WORKDIR /app
# tzdata: TZ=Asia/Dubai (in .env) sets the clock of the email sending hours; the slim image has no time zones.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . && useradd --create-home --uid 1000 openberry && mkdir -p /data && chown openberry /data

USER openberry
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

CMD ["openberry", "serve", "--host", "0.0.0.0", "--port", "8000"]
