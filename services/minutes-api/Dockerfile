FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/audio-minutes-venv \
    UV_LINK_MODE=copy \
    PATH=/opt/audio-minutes-venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

WORKDIR /app
COPY contracts /app/contracts
COPY packages/python/audio_minutes_contracts /app/packages/python/audio_minutes_contracts
COPY services/minutes-api /app/services/minutes-api
COPY deploy/container-log-runner.py /usr/local/bin/audio-minutes-log-runner.py
WORKDIR /app/services/minutes-api
RUN uv sync --frozen --no-dev

# artifacts と logs は worker と共有する volume。UID は各サービスで違うため、共通 GID
# (am-shared) + setgid (2775) で作成・読み取り・削除を成立させる。全イメージで同じ
# 所有者・mode にしておき、どのサービスが先に volume を初期化しても結果を同じにする。
# uploads は API だけが使うので共有しない (2770)。
RUN groupadd --system --gid 10100 am-shared \
    && groupadd --system --gid 10000 api \
    && useradd --system --uid 10000 --gid api --groups am-shared --home /nonexistent --shell /usr/sbin/nologin api \
    && mkdir -p /var/lib/audio-minutes/artifacts /var/lib/audio-minutes/uploads /var/log/audio-minutes \
    && chown root:am-shared /var/lib/audio-minutes/artifacts /var/log/audio-minutes \
    && chmod 2775 /var/lib/audio-minutes/artifacts /var/log/audio-minutes \
    && chown api:am-shared /var/lib/audio-minutes/uploads \
    && chmod 2770 /var/lib/audio-minutes/uploads

USER api
CMD ["python", "/usr/local/bin/audio-minutes-log-runner.py", "--service", "minutes-api", "--", "sh", "-c", "alembic upgrade head && exec uvicorn minutes_api.app:app --host 0.0.0.0 --port 8000 --no-access-log --ws none"]
