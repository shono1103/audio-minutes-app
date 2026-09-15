# transcription-worker (CPU 基準経路)。build context はリポジトリルート。
#   docker build -f services/transcription-worker/Dockerfile -t audio-minutes/transcription-worker .
# GPU・Metal を初期化しない。モデルは volume (AM_MODELS_DIR) から revision 固定で読み、通常処理は HF_HUB_OFFLINE=1。
FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app
COPY packages/python/audio_minutes_contracts /app/packages/python/audio_minutes_contracts
COPY contracts/schemas /app/contracts/schemas
COPY services/transcription-worker/pyproject.toml services/transcription-worker/README.md /app/services/transcription-worker/
COPY services/transcription-worker/uv.lock /app/services/transcription-worker/uv.lock
COPY services/transcription-worker/src /app/services/transcription-worker/src
COPY deploy/container-log-runner.py /usr/local/bin/audio-minutes-log-runner.py

WORKDIR /app/services/transcription-worker
RUN uv sync --frozen --no-dev

# artifacts / models / logs は他サービスと共有する volume。UID が違うため共通 GID
# (am-shared) + setgid (2775) にし、全イメージで同じ所有者・mode に揃える。
RUN groupadd --system --gid 10100 am-shared \
    && groupadd --system --gid 10001 worker \
    && useradd --system --uid 10001 --gid worker --groups am-shared --home /nonexistent --shell /usr/sbin/nologin worker \
    && mkdir -p /var/lib/audio-minutes/artifacts /var/lib/audio-minutes/models /var/log/audio-minutes \
    && chown root:am-shared /var/lib/audio-minutes/artifacts /var/lib/audio-minutes/models /var/log/audio-minutes \
    && chmod 2775 /var/lib/audio-minutes/artifacts /var/lib/audio-minutes/models /var/log/audio-minutes

ENV PATH="/app/services/transcription-worker/.venv/bin:${PATH}" \
    AM_ENGINE=faster-whisper \
    AM_BACKEND=cpu \
    AM_MODELS_DIR=/var/lib/audio-minutes/models \
    AM_ARTIFACTS_DIR=/var/lib/audio-minutes/artifacts \
    HF_HOME=/var/lib/audio-minutes/models \
    HF_HUB_OFFLINE=1 \
    OMP_NUM_THREADS=4

USER worker
ENTRYPOINT ["python", "/usr/local/bin/audio-minutes-log-runner.py", "--service", "transcription-worker", "--", "python", "-m", "transcription_worker"]
