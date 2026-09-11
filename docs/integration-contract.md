# 結合契約 — Compose・環境変数・volume・DB ロール

全コンポーネントがこの表に従う。変える場合はここを先に更新し、影響する README と Compose を同じ PR で直す。

## Compose (deploy/compose.yml)

| service | image | profile | 公開ポート | 備考 |
| --- | --- | --- | --- | --- |
| `db` | `postgres:16` | 共通 | なし | `POSTGRES_USER=am_api` `POSTGRES_DB=audio_minutes`。`deploy/db/init/*.sh` が `am_worker` ロールを作る |
| `minutes-api` | `audio-minutes/minutes-api` | 共通 | `127.0.0.1:8787:8000` | 起動時に Alembic migration を適用 |
| `transcription-worker` | `audio-minutes/transcription-worker` | `cpu` (既定) | なし | faster-whisper CPU INT8 |
| `transcription-worker-vulkan` | `audio-minutes/transcription-worker-vulkan` | `gpu` | なし | whisper.cpp Vulkan、`/dev/dri` のみ。`cpu` と同時に起動しない |
| `minutes-worker` | `audio-minutes/minutes-worker` | 共通 | なし | Claude CLI 同梱。内部制御 API `8791` は publish しない |

Dockerfile は `services/<name>/Dockerfile` (GPU は `services/transcription-worker/Dockerfile.vulkan`)。
build context はリポジトリルート (`audio_minutes_contracts` を COPY するため)。
Compose project 名は `audio-minutes`、`deploy/profiles/<profile>/.env` で差分を持つ。

## volume

| volume | mount 先 | 使う service |
| --- | --- | --- |
| `db-data` | `/var/lib/postgresql/data` | db |
| `artifacts` | `/var/lib/audio-minutes/artifacts` | minutes-api (rw), 両 worker (rw) |
| `uploads` | `/var/lib/audio-minutes/uploads` | minutes-api |
| `models` | `/var/lib/audio-minutes/models` | transcription-worker(-vulkan) |
| `claude-credentials` | `/var/lib/audio-minutes/claude` | minutes-worker のみ。backup 対象外 |
| `logs` | `/var/log/audio-minutes` | 全 service |

## 環境変数

共通: `AM_LOG_LEVEL` (info)、`AM_LOG_DIR=/var/log/audio-minutes`、`AM_ARTIFACTS_DIR=/var/lib/audio-minutes/artifacts`。

| 変数 | 使う service | 意味 |
| --- | --- | --- |
| `AM_DATABASE_URL` | minutes-api | `postgresql://am_api:<pw>@db:5432/audio_minutes` |
| `AM_WORKER_DATABASE_URL` | 両 worker | `postgresql://am_worker:<pw>@db:5432/audio_minutes` |
| `AM_WORKER_DB_PASSWORD` | db (init) | `am_worker` のパスワード |
| `AM_SECRET_KEY` | minutes-api | cookie 署名・CSRF。`scripts/install.sh` が生成 |
| `AM_INTERNAL_TOKEN` | minutes-api, minutes-worker | 内部 Claude 制御契約の共有秘密 |
| `AM_PUBLIC_BASE_URL` | minutes-api | `http://127.0.0.1:8787` (認可画面 URL・upload URL の生成に使う) |
| `AM_RP_ID` | minutes-api | WebAuthn RP ID。localhost 配置は `localhost` |
| `AM_ALLOWED_ORIGINS` | minutes-api | `http://127.0.0.1:8787,http://localhost:8787` |
| `AM_UPLOADS_DIR` | minutes-api | `/var/lib/audio-minutes/uploads` |
| `AM_MINUTES_WORKER_URL` | minutes-api | `http://minutes-worker:8791` |
| `AM_RETENTION_UPLOAD_HOURS` / `AM_RETENTION_AUDIO_DAYS` / `AM_RETENTION_LOG_DAYS` | minutes-api | 既定 24 / 30 / 14。owner が API で上書き |
| `AM_MAX_AUDIO_MS` / `AM_MAX_UPLOAD_BYTES` | minutes-api | 14400000 / 2147483648 |
| `AM_MODELS_DIR` | transcription-worker | `/var/lib/audio-minutes/models` (`HF_HOME` に設定) |
| `AM_ENGINE` | transcription-worker | `faster-whisper` (CPU 既定) / `whisper.cpp` |
| `AM_BACKEND` | transcription-worker | `cpu` / `vulkan`。CPU では GPU を初期化しない |
| `AM_STRATEGY` | transcription-worker | `vad_turbo` (既定) / `routed` / `whole_retry` |
| `AM_THREADS` | transcription-worker | 既定 4 |
| `AM_MODEL_JA` / `AM_MODEL_JA_REVISION` | transcription-worker | `kotoba-tech/kotoba-whisper-v2.0-faster` / `f44edd35eaeb2274e85ac7b31fb2c6f59ff1c4bc` |
| `AM_MODEL_MULTILINGUAL` / `AM_MODEL_MULTILINGUAL_REVISION` | transcription-worker | `dropbox-dash/faster-whisper-large-v3-turbo` / `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf` |
| `AM_WHISPER_CPP_BIN` / `AM_WHISPER_CPP_MODEL_DIR` | transcription-worker (whisper.cpp) | `/opt/whisper.cpp/build/bin/whisper-cli` / `/var/lib/audio-minutes/models/ggml` |
| `AM_GPU_CPU_FALLBACK` | transcription-worker | `0` (既定)。`1` なら GPU 失敗後に CPU で最大 1 回再試行 (別 attempt) |
| `HF_HUB_OFFLINE` | transcription-worker | `1` (事前取得後は外向き通信しない) |
| `AM_INTERNAL_BIND` | minutes-worker | `0.0.0.0:8791` |
| `AM_CLAUDE_BIN` | minutes-worker | `claude` |
| `AM_CLAUDE_HOME` | minutes-worker | `/var/lib/audio-minutes/claude` (`HOME` として CLI に渡す) |
| `AM_CLAUDE_MODEL` | minutes-worker | 省略時は CLI 既定 |
| `AM_CLAUDE_MOCK` | minutes-worker | `1` で mock CLI (`services/minutes-worker/mock_claude.py`) を使う。試験専用 |
| `AM_WORKER_ID` | 両 worker | 省略時は `<kind>-<hostname>` |
| `AM_LEASE_SECONDS` | 両 worker | 既定 120。heartbeat は lease の 1/3 間隔 |

## DB ロール

* `am_api`: 全テーブルの所有者。Alembic migration を実行する。
* `am_worker`: `jobs` SELECT/UPDATE、`job_events` INSERT/SELECT、`worker_heartbeats` SELECT/INSERT/UPDATE のみ。
  `contracts/sql/jobs.sql` の GRANT が正。sessions / transcripts / minutes_versions へ権限を与えない。

## クライアント設定 (macOS)

* 設定: `~/Library/Application Support/AudioMinutes/settings.json` (`deploy/settings.schema.json` で検証)
  `{ "api_base_url": "http://127.0.0.1:8787", "deploy_dir": "<repo>/deploy", "profile": "macos-colima-cpu", "docker_context": "colima" }`
* ローカル録音パッケージ: `~/Library/Application Support/AudioMinutes/sessions/<session_id>/recording/`
* 長期トークン: Keychain (service `dev.audio-minutes.client`)
* Chrome NativeBridge: Unix socket `~/Library/Application Support/AudioMinutes/bridge.sock`、
  native messaging host 名 `dev.audio_minutes.bridge`、manifest は
  `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/dev.audio_minutes.bridge.json`
* OAuth client_id: `audio-minutes-native`、redirect `http://127.0.0.1:<port>/callback`

## 版

* アプリ版: `0.1.0`。契約版: `contracts/README.md` の各 schema v1。
* 対応 Claude CLI: `2.1.268` (`services/minutes-worker/claude_compat.json`)。
* faster-whisper `1.2.1`、whisper.cpp commit `02612981545f58188a44de99b8a4710793714629`、PostgreSQL 16。
