# OpenBerry's server image (docker compose up -d). The base images are pinned by digest and every Python package comes
# from uv.lock with its hash: the same versions CI tests. Dependabot proposes updates (.github/dependabot.yml), and
# .github/workflows/docker.yml builds and runs the image on every change.

FROM astral/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c AS uv

FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS build
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
# The locked versions only (pip refuses anything without a hash from the lock), then OpenBerry without its dependencies.
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
    && python -m venv /opt/openberry \
    && /opt/openberry/bin/pip install --require-hashes -r /tmp/requirements.txt \
    && /opt/openberry/bin/pip install --no-deps .

FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH=/opt/openberry/bin:$PATH \
    OPENBERRY_HOME=/data \
    OPENBERRY_DB=/data/openberry.db

# tzdata: TZ=Asia/Dubai (in .env) sets the clock of the email sending hours; the slim image has no time zones.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/openberry /opt/openberry
RUN useradd --create-home --uid 1000 openberry && mkdir -p /data && chown openberry /data && chmod 700 /data

USER openberry
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

# On 0.0.0.0 `openberry serve` refuses to start without OPENBERRY_PASSWORD (12+ characters) unless
# OPENBERRY_ALLOW_NO_PASSWORD=true. It reads OPENBERRY_TRUSTED_PROXIES (uvicorn's forwarded_allow_ips) from the
# environment, like every other setting.
CMD ["openberry", "serve", "--host", "0.0.0.0", "--port", "8000"]
