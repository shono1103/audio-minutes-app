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

アプリケーション3サービスの stdout/stderr は Docker `json-file` (20 MiB x 7、ディスク枯渇防止) と
`logs` volume の日次 archive (`stdout/<service>/YYYY-MM-DD.log`) へ同時に出す。archive の保持期限の
正本は owner が API で変更する DB の `retention.log_days` (未設定時は環境値14日) で、
**minutes-api の定期sweepだけ**が期限切れの全serviceログを削除する。runner は削除しない。
Docker logging driver 自体は日数rotationを持たないため、容量上限と日数上限を混同しない。
`scripts/service.sh log-policy` と `scripts/doctor.sh` は Compose の両設定が一致することを診断する。

### 共有 volume の所有者と権限

サービスは非 root の別 UID で動く (minutes-api 10000 / transcription-worker 10001 /
minutes-worker 10002)。共有する `artifacts` `models` `logs` は**全イメージで同じ所有者・mode**
(`root:am-shared` = GID 10100、`2775`) にしてあり、どのサービスが先に volume を初期化しても
結果が同じになる。各ユーザーは補助グループ `am-shared` に属する。

* setgid + group 書き込みで、別 UID でも成果物・`tmp`・shard の作成/読み取り/削除ができる。
  stdout・service・日付ファイルまで `2775` / `0664` と共有 GID を明示し、API がworkerログを削除できる。
* ファイル・ディレクトリの mode は umask に依存させず `LocalArtifactStore` が明示的に付ける。
* `uploads` は minutes-api 専用 (`api:am-shared` `2770`)。
* `claude-credentials` は minutes-worker 専用 (`minutes:minutes` `0700`) で、共有 GID を与えない。
* 旧配置からの移行は `scripts/update.sh` (内部で `migrate_shared_volume_permissions`) が行う。

## 環境変数

共通: `AM_LOG_LEVEL` (info)、`AM_LOG_DIR=/var/log/audio-minutes`、`AM_ARTIFACTS_DIR=/var/lib/audio-minutes/artifacts`。

| 変数 | 使う service | 意味 |
| --- | --- | --- |
| `AM_DATABASE_URL` | minutes-api | `postgresql://am_api:<pw>@db:5432/audio_minutes` |
| `AM_WORKER_DATABASE_URL` | 両 worker | `postgresql://am_worker:<pw>@db:5432/audio_minutes` |
| `AM_WORKER_DB_PASSWORD` | db (init) | `am_worker` のパスワード |
| `AM_SECRET_KEY` | minutes-api | cookie 署名・CSRF。`scripts/install.sh` が生成 |
| `AM_INTERNAL_TOKEN` | minutes-api, minutes-worker | 内部 Claude 制御契約の共有秘密 |
| `AM_PUBLIC_BASE_URL` | minutes-api | canonical origin (`http://localhost:8787`)。認可画面 URL・upload URL をここから作る |
| `AM_RP_ID` | minutes-api | WebAuthn RP ID。**`AM_PUBLIC_BASE_URL` の host と一致必須**。IP は RP ID にできない |
| `AM_ALLOWED_ORIGINS` | minutes-api | `http://localhost:8787`。canonical origin を必ず含める |
| `AM_UPLOADS_DIR` | minutes-api | `/var/lib/audio-minutes/uploads` |
| `AM_MINUTES_WORKER_URL` | minutes-api | `http://minutes-worker:8791` |
| `AM_RETENTION_UPLOAD_HOURS` / `AM_RETENTION_AUDIO_DAYS` / `AM_RETENTION_LOG_DAYS` | minutes-api | 既定 24 / 30 / 14。DBのowner設定を正本に upload/audio/audit/application log sweepへ適用 |
| `AM_UPDATE_MAINTENANCE_FILE` | minutes-api、両worker | update中だけ置く共有 marker。API write/background taskとworker claimを停止し、health/heartbeatは継続 |
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
* `am_worker`: `jobs` SELECT/UPDATE、`job_events` INSERT/SELECT、`worker_heartbeats` SELECT/INSERT/UPDATE、
  `authorize_minutes_dispatch(uuid, integer, text)` EXECUTE のみ。
  `contracts/sql/jobs.sql` の GRANT が正。sessions / transcripts / minutes_versions へ権限を与えない。
  同関数は `SECURITY DEFINER` でjobをfencing lockし、session削除・現在owner無効化・現在Claude接続owner・
  現在の外部送信許可を原子的に再検査する。workerへ業務テーブルの直接SELECTは許可しない。

## クライアント設定 (macOS)

* 設定: `~/Library/Application Support/AudioMinutes/settings.json` (`deploy/settings.schema.json` で検証)
  `{ "api_base_url": "http://127.0.0.1:8787", "deploy_dir": "<repo>/deploy", "profile": "macos-colima-cpu", "docker_context": "colima" }`
* ローカル録音パッケージ: `~/Library/Application Support/AudioMinutes/sessions/<session_id>/recording/`
* 長期トークン: Keychain (service `dev.audio-minutes.client`)
* Chrome NativeBridge: Unix socket `~/Library/Application Support/AudioMinutes/bridge.sock`、
  native messaging host 名 `dev.audio_minutes.bridge`、manifest は
  `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/dev.audio_minutes.bridge.json`
  NativeBridgeとGUI間もnative messagingと同じ4 byte little-endian長 + JSON本体を使い、1 frameを1 MiB以下に制限する。
* OAuth client_id: `audio-minutes-native`、redirect `http://127.0.0.1:<port>/callback`

## 版

* アプリ版: `0.1.0`。契約版: `contracts/README.md` の各 schema v1。
* 対応 Claude CLI: `2.1.268` (`services/minutes-worker/claude_compat.json`)。
* faster-whisper `1.2.1`、whisper.cpp commit `02612981545f58188a44de99b8a4710793714629`、PostgreSQL 16。

## ジョブ設定 (producer / consumer)

`jobs.settings` は kind ごとに形が決まる共有契約で、`contracts/schemas/job.v1.schema.json`
の `$defs` と `audio_minutes_contracts.models` の `TranscriptionJobSettings` /
`MinutesJobSettings` が正。producer は minutes-api、consumer は各 worker。

* minutes は `allow_external_send` と `connected_owner_id` を必ず載せる。worker は送信直前に
  この 2 つと実際の Claude ログイン状態を再検査し、欠けている payload は拒否する。
* タイトルのキーは `title` で統一する。`title_edited_by_user` が false のときだけ
  worker が Claude の提案タイトルを採用する。
* 接続 owner の変更・ログアウト時は、API が合致しない議事録ジョブを取り消す
  (worker は投入時のスナップショットしか見られないため)。
* `transcript_revision` は「作ろうとしている版」で、投入の冪等キー (`idempotency_key`) とは別。
  利用者の再処理要求ごとに `sessions.transcription_generation` / `minutes_generation` を進める。
* 録音中chunkのtranscription jobは `live_chunk_sequence` と `live_chunk_duration_ms` を持つ。
  reconcilerは成果物を公開せずchunkへ仮関連付けし、完全WAVのfinalize後に連続性と両trackを
  検証して最終Transcriptを一度だけ公開する。仮Transcriptからminutes jobは投入しない。
