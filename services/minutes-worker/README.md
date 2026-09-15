# minutes-worker

Claude CLI (個人向け subscription) で議事録 Markdown を生成する worker。
Claude の認証と資格情報アクセスはこの worker の `ClaudeAuthAdapter` だけが行う (FR-120〜132、ADR-0005)。

## 構成

| module | 役割 |
| --- | --- |
| `claude_auth.py` | `claude auth status/login/logout`。ログイン URL はメモリだけに保持し 5 分で失効。同時ログイン 1 件 |
| `control_api.py` | 内部制御 API (`contracts/internal/claude-control.md`)。`X-Internal-Token` 必須。Compose 内部限定 |
| `claude_generate.py` | `claude -p --output-format json --json-schema ...` を配列引数・stdin・allowlist 環境で起動し結果を分類 |
| `prompting.py` | 固定安全指示、`<transcript>` 等の区切り、長文分割 (chunk → 中間メモ → 統合)、構造検査 |
| `render.py` | FormatProfile テンプレートの `{{title}} {{started_at}} {{duration}} {{section:<key>}}` 置換と HTML 無害化 |
| `runner.py` | jobs キューの claim → 送信前再検査 → 生成 → 検証 → artifact 公開 → complete/fail |
| `mock_claude.py` | 実 CLI と同じ引数・出力形式の模倣 (`AM_CLAUDE_MOCK=1`)。試験専用 |
| `claude_compat.json` | 対応 CLI 版と確認済みオプション・出力キー。対応表に無い版では推測フローを実行しない |

## Claude 接続の状態遷移

```text
cli_missing / cli_incompatible / credential_conflict ─ (CLI・環境を直す) ─▶ logged_out
logged_out ─ login ─▶ login_pending(pending → url_ready) ─ owner がブラウザーで認証 ─▶ logged_in
logged_in ─ 失効 ─▶ expired ─ login ─▶ …      logged_in ─ 利用上限 ─▶ rate_limited (5 分後に再確認)
login_pending ─ cancel ─▶ cancelled / 5 分経過 ─▶ expired / allowlist 外 URL ─▶ failed
```

ログイン: `pending | url_ready | completed | failed | cancelled | expired`。URL は `url_ready` の間だけ
`GET /internal/v1/claude/login/{id}` の応答に含まれ、DB・ログ・例外・監査へは書かない。
コンテナに OAuth callback が届かない配置向けに `POST /internal/v1/claude/login/{id}/code` (契約外の拡張) で
owner が受け取った認可コードを CLI へ渡せる。コードは書き込むだけで保持しない。

## 送信前の安全条件 (runner)

1. `settings.allow_external_send` が false → `external_send_forbidden` (外部送信禁止セッション)
2. `settings.connected_owner_id` ≠ `job.owner_id` → `not_connected_owner`
3. `status()` が `credential_conflict` → `claude_credential_conflict` (変数名だけ通知、値は読まない)
4. `status()` が `logged_in` 以外 → `claude_not_authenticated` (retryable)
5. 生成後は JSON schema と構造検査 (必須セクション・title・参照 segment ID の実在) を通してから artifact にする
6. timeout などで応答を失った場合は `outcome="unknown"` で complete し、自動再送しない

API 側は `settings` に `allow_external_send`、`connected_owner_id`、`session_title`、`title_edited_by_user`、
`started_at`、`duration_ms`、`instructions` (再生成時)、任意で `format_snapshot` を入れる。
入力 artifact は `transcript_json` (必須)、`format_snapshot` (任意、settings 優先)、`minutes_md` (再生成の基準版)。

## 実行

```sh
uv sync --extra dev
uv run pytest -q                       # mock CLI による単体試験
uv run pytest -q -m integration        # Docker の postgres:16 を一時起動して runner を通す
AM_CLAUDE_MOCK=1 AM_MOCK_CLAUDE_SCENARIO=logged_in AM_INTERNAL_TOKEN=dev AM_WORKER_DATABASE_URL=... uv run minutes-worker
```

## 実ログイン手順 (owner)

1. GUI「設定 > Claude 接続」または CLI `audio-minutes claude login` が `POST /v1/admin/claude/login` を呼ぶ。
2. minutes-api が内部制御 API へ中継し、`url_ready` になった URL を owner だけに表示する。
3. owner がシステムブラウザーで Anthropic にログインする。コンテナへ callback が届かない場合は表示されたコードを
   GUI/CLI から送り、`/code` 拡張で CLI に渡す。
4. `completed` 後、`claude auth status --json` の `loggedIn=true` かつ `authMethod=claude.ai` を確認して `logged_in` になる。

`--console` (API 課金) は使わない。`ANTHROPIC_API_KEY` 等が存在すると値を読まずに停止する。

## 未検証

* linux/arm64 コンテナ内での実ログイン (owner の操作が必要)。URL 提示形式は mock と 2.1.268 の実 `auth status --json` 出力で確認。
* 実 Claude への送信は行っていない。生成経路は mock CLI と、実 CLI の未認証時 JSON 応答で確認。
