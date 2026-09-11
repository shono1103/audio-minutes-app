# ADR-0005 Claude CLI の起動方法と安全条件

* 状態: 採用 (2026-09-12、対象 CLI 2.1.268)
* 対応: 全体計画 M5 安全条件、FR-120〜132

## 決定

* 生成は `claude -p --output-format json --tools "" --strict-mcp-config --mcp-config '{}' --disable-slash-commands --setting-sources "" --no-session-persistence --max-turns 1 --permission-mode dontAsk --system-prompt <固定安全指示> --json-schema <議事録スキーマ>` を
  配列引数で起動し、本文は stdin から渡す。shell 展開・一時ファイル経由の本文露出をしない。
  `--bare` は OAuth を読まないため subscription profile では使わない。
* 認証は `claude auth login --claudeai` を PTY で起動し、標準出力から HTTPS URL を抽出して
  allowlist origin と照合する。`claude auth status --json` で `loggedIn` 相当を確認する。
* `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX` /
  `CLAUDE_CODE_USE_FOUNDRY` 等の存在を検出したら値を読まず `claude_credential_conflict` で停止する。
* 長文は概算 token 予算 (既定 60,000 文字/chunk) で segment 境界に分割し、中間要約に根拠 segment ID を残す。
* 出力は JSON schema で構造検証し、必須セクション・タイトル・参照 segment ID を検査してから版にする。
* 対応 CLI 版は `services/minutes-worker/claude_compat.json` に記録し、未対応版では推測フローを実行しない。

## 未確認

コンテナ (linux/arm64) 内での実ログインは owner の操作が必要なため未実施。mock CLI で全状態を試験する。
