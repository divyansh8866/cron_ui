FROM python:3.12-slim

# Tools commonly needed by scheduled scripts:
#   bash       -> scripts with `#!/usr/bin/env bash` and bashisms (set -Eeuo pipefail)
#   util-linux -> flock (single-instance locks) and nsenter (host-level root)
#   zip/unzip  -> archive handling / zip integrity checks
#   tzdata     -> per-job timezones
# (The Docker CLI is installed separately below as a static binary, since the
#  Debian `docker.io` package is unreliable across base-image releases.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tzdata \
        ca-certificates \
        curl \
        bash \
        rsync \
        util-linux \
        zip \
        unzip \
        less \
        jq \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI (client only) + AWS CLI v2, both arch-aware so the image builds on
# amd64 (Zimaboard/x86) and arm64 hosts. The Docker CLI talks to the host daemon
# through the mounted /var/run/docker.sock — no daemon runs inside this image.
ARG DOCKER_CLI_VERSION=27.5.1
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) dl_arch=x86_64 ;; \
      arm64) dl_arch=aarch64 ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    # Docker CLI (static): extract only the `docker` client binary.
    curl -fsSL "https://download.docker.com/linux/static/stable/${dl_arch}/docker-${DOCKER_CLI_VERSION}.tgz" -o /tmp/docker.tgz; \
    tar -xzf /tmp/docker.tgz --strip-components=1 -C /usr/local/bin docker/docker; \
    rm -f /tmp/docker.tgz; \
    docker --version; \
    # AWS CLI v2 (native).
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${dl_arch}.zip" -o /tmp/awscliv2.zip; \
    unzip -q /tmp/awscliv2.zip -d /tmp; \
    /tmp/aws/install; \
    rm -rf /tmp/aws /tmp/awscliv2.zip; \
    aws --version

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend
COPY frontend /app/frontend

ENV CRON_UI_DATA=/data \
    PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8080/api/health || exit 1

WORKDIR /app/backend
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
