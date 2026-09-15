# Claude 認証制御契約 (minutes-api → minutes-worker、内部ネットワーク限定)

Claude の認証 URL・認可codeは DB・キュー・ログ・トレースへ書かない。minutes-worker がURLだけをプロセス内メモリに短命保持し、
minutes-api が要求 owner へ中継する。状態 ID だけを DB に永続化し、worker 再起動で認証待ちは失効する。

* ベース: `http://minutes-worker:8791` (Compose 内部ネットワークのみ。ホストへ publish しない)
* 認証: ヘッダー `X-Internal-Token: <AM_INTERNAL_TOKEN>` (Compose secret / env で API と worker に同じ値)
* 同時ログイン処理は配置全体で 1 件。2 件目は 409 `conflict`

| メソッド / パス | 応答 |
| --- | --- |
| `GET /internal/v1/claude/status` | `{state, cli_version, cli_supported, checked_at, conflict_env_vars: [名前のみ], detail?}` |
| `POST /internal/v1/claude/login` | 201 `{auth_session_id, expires_at}` |
| `GET /internal/v1/claude/login/{id}` | `{state, url?, expires_at, failure_code?}`。`url` は `url_ready` の間だけ、`failure_code` は安全な分類のみ |
| `POST /internal/v1/claude/login/{id}/code` | body=`{code}`、`{auth_session_id, state}`。PTYへ一度だけ渡し、保持・log出力しない |
| `POST /internal/v1/claude/login/{id}/cancel` | 200 `{state: cancelled}` |
| `POST /internal/v1/claude/logout` | 200 `{state}` |
| `GET /internal/v1/health` | `{status, worker: "minutes-worker", version}` |

`state` (status): `cli_missing | checking | logged_out | login_pending | logged_in | expired | credential_conflict | rate_limited | cli_incompatible`
`state` (login): `pending | url_ready | completed | failed | cancelled | expired`

URL は HTTPS かつ allowlist (`https://claude.ai/`, `https://console.anthropic.com/`, `https://platform.claude.com/`) の
origin に一致した場合だけ `url_ready` にする。一致しない場合は `failed` (`claude_cli_incompatible`)。
ログイン開始・完了・失敗・取消・ログアウトは会議内容を含めず監査ログへ記録する (API 側)。
