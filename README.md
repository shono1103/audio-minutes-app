# audio-minutes-app

macOS 上で Zoom / Microsoft Teams / Google Chrome (Google Meet タブ) の音声とマイクを同期録音し、
Colima VM 上の Docker コンテナで自己ホストした Whisper 系モデルにより文字起こしし、
Claude CLI で構造化された Markdown 議事録を生成するシステムの実装リポジトリ。

要件・計画・タスク・手動テスト手順は管理リポジトリ (audio-minutes) が正本で、
このリポジトリには実装・契約・デプロイ・ADR・install スキルを置く。

構成と使い方は追って `docs/` と各ディレクトリの README に記載する。
