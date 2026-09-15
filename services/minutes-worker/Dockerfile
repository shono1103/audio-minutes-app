# minutes-worker: Claude CLI (固定版) + Python ワーカー。build context はリポジトリルート。
# 資格情報は AM_CLAUDE_HOME (専用 volume) にだけ置き、ホストの ~/.claude を mount しない。
#
# Python は project の requires-python (>=3.12) に合わせて python:3.12-slim-bookworm を基準にする。
# Bookworm の apt python3 は 3.11 なので使わない。Claude CLI 用の Node は同じ bookworm の
# 公式イメージから runtime だけを取り込み、版を固定する (glibc 世代を揃えるため bookworm 同士にする)。
ARG NODE_VERSION=22
FROM node:${NODE_VERSION}-bookworm-slim AS node
FROM python:3.12-slim-bookworm AS base

ARG CLAUDE_CODE_VERSION=2.1.268
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm

RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force \
    && node --version && python3 --version && claude --version

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

# 共有 volume (artifacts / logs) は全サービス共通の GID で 2775 にし、UID が違っても
# 作成・読み取り・削除ができるようにする。資格情報だけは minutes-worker 専用 (0700)。
RUN groupadd --system --gid 10100 am-shared \
    && groupadd --system --gid 10002 minutes \
    && useradd --system --uid 10002 --gid minutes --groups am-shared \
       --home-dir /var/lib/audio-minutes/claude --create-home minutes \
    && mkdir -p /var/lib/audio-minutes/artifacts /var/log/audio-minutes /opt/audio-minutes \
    && chown root:am-shared /var/lib/audio-minutes/artifacts /var/log/audio-minutes \
    && chmod 2775 /var/lib/audio-minutes/artifacts /var/log/audio-minutes \
    && chown minutes:minutes /var/lib/audio-minutes/claude /opt/audio-minutes \
    && chmod 0700 /var/lib/audio-minutes/claude

WORKDIR /opt/audio-minutes
COPY --chown=minutes:minutes packages/python/audio_minutes_contracts packages/python/audio_minutes_contracts
COPY --chown=minutes:minutes contracts/schemas contracts/schemas
COPY --chown=minutes:minutes contracts/fixtures/format-profile.standard.json contracts/fixtures/format-profile.standard.json
COPY --chown=minutes:minutes services/minutes-worker services/minutes-worker
COPY deploy/container-log-runner.py /usr/local/bin/audio-minutes-log-runner.py

USER minutes
WORKDIR /opt/audio-minutes/services/minutes-worker
RUN uv sync --frozen --no-dev --python /usr/local/bin/python3 \
    && .venv/bin/python -c "import sys; assert sys.version_info >= (3, 12), sys.version"

ENV PATH="/opt/audio-minutes/services/minutes-worker/.venv/bin:${PATH}" \
    AM_CLAUDE_BIN=claude \
    AM_CLAUDE_HOME=/var/lib/audio-minutes/claude \
    AM_ARTIFACTS_DIR=/var/lib/audio-minutes/artifacts \
    AM_INTERNAL_BIND=0.0.0.0:8791 \
    HOME=/var/lib/audio-minutes/claude

VOLUME ["/var/lib/audio-minutes/claude"]
EXPOSE 8791

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8791/internal/v1/health', timeout=3).status == 200 else 1)"

CMD ["python3", "/usr/local/bin/audio-minutes-log-runner.py", "--service", "minutes-worker", "--", "minutes-worker"]
