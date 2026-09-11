# audio-minutes-app

macOS 上で Zoom / Microsoft Teams / Google Chrome (Google Meet タブ) の音声とマイクを同期録音し、
Colima VM 上の Docker コンテナで自己ホストした Whisper 系モデルにより文字起こしし、
Claude CLI (個人 subscription) で構造化された Markdown 議事録を生成するシステムの実装リポジトリ。

要件・計画・タスク・Gherkin 手順書の正本は管理リポジトリ `audio-minutes` にある。
ここには実装・契約・デプロイ・ADR・install スキルを置く。全体像は [docs/architecture.md](docs/architecture.md)。

## 構成

```text
apps/macos/                    # SwiftUI App (録音・取り込み・GUI・メニューバー) と NativeBridge (Chrome 連携 host)
apps/cli/                      # audio-minutes CLI (取り込み・セッション・議事録版・形式・Claude 接続・service)
packages/swift/                # ClientCore / ImportCore / RecorderCore (View 非依存)
packages/python/audio_minutes_contracts/  # 契約の Python 写しと Queue / ArtifactStore アダプター
extensions/chrome/             # Google Meet タブ連携拡張 (MV3, TypeScript)
services/minutes-api/          # FastAPI: 認証・セッション・tus・ジョブ制御・成果物・版管理・bootstrap CLI
services/transcription-worker/ # faster-whisper CPU / whisper.cpp (Vulkan) アダプターと共通戦略
services/minutes-worker/       # Claude CLI による議事録生成と Claude 認証アダプター
contracts/                     # JSON Schema・fixture・API 一覧・内部制御契約・jobs DDL
deploy/                        # Docker Compose、profile、Dockerfile、固定イメージ情報
scripts/                       # doctor / install / update / backup / restore / service の実体
tests/                         # 契約・統合試験と合成音声 fixture
benchmarks/                    # CPU / GPU 共通の評価ハーネス (実会議データは Git 管理外)
docs/adr/                      # 設計判断記録
.claude/skills/install/        # Codex / Claude Code 共用 install スキル (.agents/skills はリンク)
```

## 疎結合の規則

* API・各 worker は別パッケージ・別 lockfile・別イメージ。worker 間の直接呼出しは禁止。
* 共有するのは `contracts/` と `audio_minutes_contracts` (schema の写し、Queue / ArtifactStore) と fixture まで。
  API の ORM・業務ロジックを worker へ import しない。
* 業務テーブルは API (`am_api`) が所有し、worker は `am_worker` ロールで jobs / job_events だけを操作する。
* 成果物は artifact ID で参照し、クライアントのパスや URL を worker のアクセス先にしない。
* Claude 認証 URL は minutes-worker のメモリだけに置き、DB・キュー・ログへ書かない。

## クイックスタート (macOS + Colima)

```sh
# 1. ホスト検査 (読み取り専用)
./scripts/doctor.sh

# 2. 導入 (既定 profile: macos-colima-cpu)。Claude Code なら /install、Codex なら $install でも同じ手順
./scripts/install.sh

# 3. 初回 owner の登録 URL を発行 (10 分・1 回限り)
./scripts/service.sh bootstrap

# 4. CLI からログインし、音声ファイルを取り込む
swift run --package-path apps/cli audio-minutes auth login
swift run --package-path apps/cli audio-minutes import meeting.m4a --title "週次" --language auto
```

各ディレクトリの README に詳細を置く。会議音声・文字起こし・議事録・Claude 資格情報は Git に入れない。

## 開発

| 対象 | コマンド |
| --- | --- |
| 契約テスト | `uv run --project packages/python/audio_minutes_contracts pytest` |
| API | `cd services/minutes-api && uv sync && uv run pytest` |
| transcription-worker | `cd services/transcription-worker && uv sync && uv run pytest` |
| minutes-worker | `cd services/minutes-worker && uv sync && uv run pytest` |
| Swift | `swift build --package-path packages/swift && swift test --package-path packages/swift` |
| Chrome 拡張 | `cd extensions/chrome && npm ci && npm test && npm run build` |
| Compose (CPU) | `docker compose -f deploy/compose.yml --profile cpu up -d --build` |

## ライセンス

MIT。依存 OSS のライセンスは各パッケージの lockfile と `deploy/THIRD_PARTY.md` を参照。
