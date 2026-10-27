# syntax=docker/dockerfile:1
# Optional migration image. Build and publish a multi-platform manifest, then set
# config.runner_image to its digest. The infrastructure works with the stock image.
FROM ghcr.io/actions/actions-runner:2.338.2@sha256:759a1d94a15b05941aba5196974ff43fe15b4cbba29b694b35cbc32a1166b232
USER root
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential cmake ninja-build clang lld ccache pkg-config shellcheck \
    libeigen3-dev libmpfr-dev libssl-dev libffi-dev libpq-dev default-libmysqlclient-dev \
    python3 python3-dev python3-venv python-is-python3 \
    curl ca-certificates git jq unzip zip zstd rsync gettext tzdata tzdata-legacy \
    libasound2t64 libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 libdrm2 libgbm1 \
    libnspr4 libnss3 libpango-1.0-0 libcairo2 libx11-6 libxcb1 libxcomposite1 \
    libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 fonts-liberation \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/hostedtoolcache \
    && chown 1001:123 /opt/hostedtoolcache
ENV RUNNER_TOOL_CACHE=/opt/hostedtoolcache AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache
USER runner
