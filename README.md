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

MVP の cold install 対象は **macOS 14.2 以降**で、Homebrew と Xcode 15.3 以降
(Swift 5.10 を含む Xcode または Command Line Tools) の導入は事前の本人操作が必要。
Homebrew 自体、Xcode／Command Line Tools、OS 権限ダイアログを installer は操作しない。
これらの前提が揃ったホストでは、リポジトリ直下の `install.sh` 1回で不足 formula の確認・
専用 Colima・server・CPUモデル・macOS client の配置と readiness までを行い、中断時は同じコマンドで再開できる。

```sh
# 1. ホスト検査 (読み取り専用)
./scripts/doctor.sh

# 2. 導入 (既定 profile: macos-colima-cpu)。Claude Code なら /install、Codex なら $install でも同じ手順
./install.sh

# tailnet 内の別端末からも使う場合（Tailscale はログイン済みであること）
./install.sh --tailscale

# 3. 初回 owner の登録 URL を発行 (10 分・1 回限り)
./scripts/service.sh bootstrap

# 4. CLI からログインし、音声ファイルを取り込む
swift run --package-path apps/cli audio-minutes auth login
swift run --package-path apps/cli audio-minutes import meeting.m4a --title "週次" --language auto
```

別の macOS へ現在の worktree（未コミット変更と非 ignored 新規ファイルを含む）を導入する場合は、
その Mac に直接ログインできるローカル端末から次を実行する。既定の接続先は
`shonoshono@192.168.0.31`、固定配置先は接続ユーザーの
`~/.local/share/audio-minutes/repository` である。

```sh
./scripts/install-remote.sh
# 接続先を変える場合（配置先と profile は安全のため固定）
./scripts/install-remote.sh --host user@host.example
# 導入後に Tailscale Serve の HTTPS 入口も設定
./scripts/install-remote.sh --tailscale
```

接続前に転送元・除外対象・導入内容を表示し、接続後は hostname、OS、model、arch、memory を
表示する。表示された hostname を含む確認文字列を入力してから転送する。SSH はホスト鍵を
自動承認せず、通常の OpenSSH 認証順を使うため、公開鍵が無ければパスワードを端末へ直接入力する。
`sshpass` や資格情報ファイルは使わない。接続を共有しないため、パスワード認証では正常系で最大4回
（preflight 検査・lock 取得、source 転送・実行）の入力が必要になる。既存配置は専用管理マーカーがある場合だけ
更新し、実 `.env` を byte 単位で保持する。所有者付き lock で同時実行を拒否し、通常の失敗時は lock を
解放する。接続断で lock が残った場合は、表示された owner と同時実行が無いことを SSH 上で確認してから
owner ファイルと空 lock ディレクトリを削除する。失敗時に表示される stage、log、再開案内を使って
調査・再実行できる。

`--tailscale` は API の Docker publish を `127.0.0.1` のまま維持し、Tailscale Serve の
HTTPS 443 だけを入口にする。既存 Serve/Funnel が空、または同じホスト・port・upstream の
audio-minutes 単独設定である場合に限って続行する。別の Serve、Funnel、TCP forward、path handler は
reset・上書きせず拒否する。Tailscale DNS 名に合わせて WebAuthn の canonical origin を変更する前に
登録済み passkey の有無を検査し、存在する場合は自動移行しない。設定失敗時は `.env` と ClientSettings、
minutes-api を元へ戻し、この実行で新設した Serve だけを解除する。

preflight 検査と lock 取得は script を標準入力で渡すため remote PTY を割り当てず、`/bin/bash -s` が
EOF で確実に終了する。SSH パスワードはこの場合も OpenSSH がローカル端末へ直接問い合わせる。source
転送後の installer 実行は remote file を `/bin/bash` で起動し、進捗表示のため PTY を割り当てる。

転送 archive は link を含めない。repo 内 symlink は現在利用する `scripts/inspect-host.sh` と
`packages/python/audio_minutes_contracts/schemas` の2つだけを解決先込みで許可し、ローカル stage に
通常ファイル／ディレクトリとして実体化する。解決先が repo 外、壊れている、循環している、または
途中の祖先が symlink の場合は SSH 接続前に拒否する。
検証済みの転送 source は directory `0755`、実行対象 file `0755`、その他 file `0644` に正規化し、
Docker image 内の非 root runtime から読める状態にする。`.env`、管理 marker、remote の管理用親ディレクトリは
この正規化の対象外で、`0600`／`0700` を維持する。

導入は専用 Colima profile、3つのアプリケーション image、固定 revision のCPUモデル、DB migration、
Swift CLI / `.app` / NativeBridge、Chrome開発版拡張を準備し、業務 readiness まで待つ。既存設定と
永続データは再実行時も保持する。owner登録、Claudeログイン、macOS録音権限、Chromeへのunpacked
拡張読込は本人操作が必要なため、終了時に `PENDING` として案内される。

Homebrew が導入済みの macOS では、不足している `docker` / `docker-compose` / `colima` / `jq` /
`node` だけを一覧表示し、同意後に `brew install` する (`--yes` はその同意を含む)。Xcode / Swift
など OS 管理の導入は自動操作せず、機械可読な `PENDING` と非 zero で止まる。Linux profile でも
package manager は自動実行しない。Colima の新規・停止中 profile は `--activate=false` で起動し、
global Docker context を変えない。`linux-cpu` は Ubuntu 22.04 LTS以降／x86_64 の将来向け雛形で、
MVP の cold install 完了対象ではない。GPU profile も評価用であり、別途 krunkit/Vulkan の準備と許可が必要。

更新は `./scripts/service.sh update --backup-dir <新規ディレクトリ>` を使う。旧 image と backup を
退避してから、image build、モデル完全性／offline smoke、client build、業務 readiness を一続きで
確認する。整合 backup 後は maintenance marker により API write／background task／worker claim を止め、
readiness 成功時の marker 削除を更新の commit point にする。途中で失敗した場合は write を止めたまま
旧 image と DB／artifact backup を戻し、rollback 完了後に旧サービスを再開する。

minutes-api の bootstrap・招待・再認証・パスキー登録 URL には一回限り token が含まれる。
このため Uvicorn の生 access log はサポート対象の全起動経路で無効にし、request target と query を
Docker の stdout archive に残さない。アプリは WebSocket を使わないため配置時はWebSocket対応も無効にし、
直接Uvicornを起動した場合もhandshakeのrequest-bearing logだけを除く。秘密を含めない
application／audit log と起動・停止・一般エラーログは維持する。

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
