# 主要な依存 OSS とライセンス

配布前に各パッケージの lockfile (`uv.lock`, `package-lock.json`, `Package.resolved`) と照合して更新する。
モデルの重みはイメージに含めず、導入時に revision 固定で取得する。

| コンポーネント | 依存 | ライセンス | 備考 |
| --- | --- | --- | --- |
| minutes-api | FastAPI, Starlette, Uvicorn | MIT / BSD-3 | |
| minutes-api | SQLAlchemy, Alembic | MIT | |
| minutes-api | psycopg 3 | LGPL-3.0 | 動的リンク利用 |
| minutes-api | argon2-cffi | MIT | Argon2id |
| minutes-api | py_webauthn | BSD-3 | パスキー |
| minutes-api | pydantic, jsonschema | MIT | |
| transcription-worker | faster-whisper | MIT | CPU INT8 |
| transcription-worker | CTranslate2 | MIT | |
| transcription-worker | Silero VAD (faster-whisper 同梱 ONNX) | MIT | |
| transcription-worker | onnxruntime | MIT | |
| transcription-worker (GPU) | whisper.cpp / ggml | MIT | commit 固定 |
| transcription-worker (GPU) | Mesa (Venus / Vulkan loader) | MIT 系 | linux/arm64 ゲスト。追加配布元を使う場合はここに明記 |
| transcription-worker | ffmpeg | LGPL-2.1+ (構成により GPL) | Debian パッケージ |
| minutes-worker | Claude Code CLI (`@anthropic-ai/claude-code`) | Anthropic 商用利用条件 | subscription OAuth の第三者利用条件を配布前に再確認 |
| minutes-worker | Node.js | MIT | CLI 実行環境 |
| DB | PostgreSQL 16 | PostgreSQL License | |
| クライアント | swift-argument-parser | Apache-2.0 | |
| Chrome 拡張 | esbuild, TypeScript, vitest (開発時のみ) | MIT / Apache-2.0 | 配布物に含まれない |
| モデル | kotoba-tech/kotoba-whisper-v2.0-faster | Apache-2.0 | 日本語 |
| モデル | dropbox-dash/faster-whisper-large-v3-turbo (openai/whisper-large-v3-turbo) | MIT | 英語・多言語フォールバック |
| モデル | Systran/faster-whisper-large-v3 | MIT | 精度比較基準 (評価時のみ) |
| モデル | Systran/faster-distil-whisper-large-v3 | MIT | 速度比較候補 (評価時のみ) |

Claude Code CLI は OSS ではなく Anthropic の利用条件に従う。個人 subscription を localhost で本人が使う MVP に限定し、
配布・複数ユーザー生成・リモートサービス化の前に規約と認証ドキュメントを再確認する (docs/adr/0005)。
