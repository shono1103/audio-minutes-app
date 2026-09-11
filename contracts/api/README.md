# minutes-api HTTP 契約 (v1)

ベース URL: localhost では `http://127.0.0.1:8787` (loopback のみ bind)。リモートは HTTPS 必須。
`/v1/*` は Bearer アクセストークン、`/auth/*` と `/oauth/*` はブラウザー向け HTML / OAuth。
HTTP 受付は 202 + ID を返し、decode や推論の完了を待たない。エラーは `error.v1.schema.json`。

## 認証・認可

| 操作 | メソッド / パス | 備考 |
| --- | --- | --- |
| bootstrap 登録画面 | `GET/POST /auth/bootstrap/{token}` | サーバー CLI `minutes-api bootstrap` が発行した 10 分 / 1 回限りのトークン。hash だけ保存 |
| 招待消費 | `GET/POST /auth/invite/{token}` | 招待 email と一致する登録だけ許可 |
| ログイン画面 | `GET/POST /auth/login` | パスワード (Argon2id、15 文字以上、頻出拒否) / パスキー |
| WebAuthn | `POST /auth/webauthn/{register,login}/{options,verify}` | py_webauthn。RP ID は固定 (`localhost` または設定 host) |
| 認可 | `GET /oauth/authorize` | `client_id=audio-minutes-native` `redirect_uri=http://127.0.0.1:<port>/callback` (RFC 8252 loopback、任意 port) `code_challenge_method=S256` `state` |
| トークン | `POST /oauth/token` | `authorization_code` (PKCE 必須) / `refresh_token` (ローテーション、再使用検知で系列失効) |
| 失効 | `POST /oauth/revoke` | refresh 系列を失効 |
| 再認証 | `POST /v1/auth/reauth` | 招待発行・権限変更・Claude 接続操作の前に必要。5 分有効 |
| 自分 | `GET /v1/me` | role、reauth 状態 |
| リカバリー | `POST /v1/account/recovery-codes` | 再発行。表示は 1 回 |
| パスキー | `GET /v1/account/passkeys`, `DELETE /v1/account/passkeys/{id}` | |

アクセストークンは 15 分の opaque トークン (hash 保存)、リフレッシュは 30 日・使用ごとローテーション。
ログイン・招待・bootstrap にはレート制限。アカウント有無を推測しにくい応答にする。

## セッションと処理

| 操作 | メソッド / パス | 認可 |
| --- | --- | --- |
| 作成 | `POST /v1/sessions` body=`recording-package.v1` | 認証ユーザー。`session_id` で冪等。必要トラック分の tus upload URL を返す (`session.v1`) |
| 一覧 | `GET /v1/sessions?cursor=&limit=` | 所有 + 共有先 |
| 取得 | `GET /v1/sessions/{id}` | 所有者 / 共有先 |
| 更新 | `PATCH /v1/sessions/{id}` `{title?, language_mode?, allow_external_send?}` | 所有者のみ。title 更新で `title_edited_by_user=true`。`allow_external_send` は false→true を暗黙に許さない (明示 body 必須) |
| 削除 | `DELETE /v1/sessions/{id}` | 所有者。先にアクセス停止・attempt 無効化、後で成果物削除 |
| 確定 | `POST /v1/sessions/{id}/finalize` | 所有者。全必須トラックの完了・サイズ・実形式・長さ・checksum 検証後に一度だけ transcription job 投入。二重 finalize は同じ結果 |
| 再処理 | `POST /v1/sessions/{id}/retry` `{stage: transcription|minutes, language_mode?}` | 所有者。新 revision / attempt 群。音声削除済みなら `audio_deleted` |
| ジョブ | `GET /v1/sessions/{id}/jobs` | 所有者 / 共有先 (状態のみ) |
| 共有 | `GET/POST /v1/sessions/{id}/shares`, `DELETE /v1/sessions/{id}/shares/{user_id}` | 所有者。登録済みユーザーのみ、閲覧権限のみ |

### tus 1.0 (Core / Creation / Expiration / Termination)

| 操作 | メソッド / パス |
| --- | --- |
| 作成 | `POST /v1/sessions/{id}/uploads` `Upload-Length`, `Upload-Metadata: track_id <base64>` |
| 状態 | `HEAD /v1/uploads/{upload_id}` → `Upload-Offset`, `Upload-Length`, `Upload-Expires` |
| 追送 | `PATCH /v1/uploads/{upload_id}` `Content-Type: application/offset+octet-stream`, `Upload-Offset` |
| 取消 | `DELETE /v1/uploads/{upload_id}` |
| 能力 | `OPTIONS /v1/uploads` → `Tus-Version: 1.0.0`, `Tus-Extension: creation,expiration,termination` |

全操作でセッション所有者の Bearer を検証する。`Upload-Metadata` はファイル名・パス決定に使わない。
未完了 upload は 24 時間で失効。受信済み offset がサーバー側の正。

### 成果物

| 操作 | メソッド / パス |
| --- | --- |
| 音声 | `GET /v1/sessions/{id}/audio/{track_id}` (Range 対応、所有者 / 共有先) |
| 文字起こし | `GET /v1/sessions/{id}/transcript` (`transcript.v1`), `GET /v1/sessions/{id}/transcript.md` |
| 現在版議事録 | `GET /v1/sessions/{id}/minutes` → メタ + Markdown |
| 版履歴 | `GET /v1/sessions/{id}/minutes/versions` (所有者のみ) |
| 版取得 | `GET /v1/sessions/{id}/minutes/versions/{vid}` (所有者のみ) |
| 手動編集版 | `POST /v1/sessions/{id}/minutes/versions` `{parent_version_id, body_markdown, expected_current_version_id}` (競合検査) |
| 再生成 | `POST /v1/sessions/{id}/minutes/regenerate` `{base_version_id, instructions, format_profile_id? | use_snapshot: true}` → 202 job |
| 現在版選択 | `POST /v1/sessions/{id}/minutes/current` `{version_id}` |
| 復元 | `POST /v1/sessions/{id}/minutes/versions/{vid}/restore` → 新しい版 |
| 比較 | `GET /v1/sessions/{id}/minutes/compare?from=&to=` → unified diff |

### フォーマット

`GET/POST /v1/formats`, `GET/PUT/DELETE /v1/formats/{id}`, `POST /v1/formats/{id}/duplicate`,
`POST /v1/formats/{id}/default`, `POST /v1/formats/preview` (副作用なし、サンプルデータで Markdown を返す)。
本人のプロファイルだけ。組み込み標準 (`builtin=true`) は編集・削除不可で複製のみ。

### 管理 (owner、reauth 必須)

`GET/POST /v1/admin/invitations`, `DELETE /v1/admin/invitations/{id}`, `GET /v1/admin/users`,
`PATCH /v1/admin/users/{id}` `{role?, disabled?}` (最後の owner は降格・無効化不可),
`GET/PUT /v1/admin/retention`, `GET /v1/admin/audit?cursor=`,
`GET /v1/admin/claude`, `POST /v1/admin/claude/login`, `GET /v1/admin/claude/login/{id}`,
`POST /v1/admin/claude/login/{id}/cancel`, `POST /v1/admin/claude/logout`。

### health / capabilities

`GET /v1/health` (未認証、`{status, version}` のみ)、`GET /v1/capabilities` (認証、DB/queue/worker heartbeat/
モデル準備/契約版/backend/GPU 状態)。ポーリング間隔の推奨は `Retry-After` または `poll_interval_ms` で返す (既定 3000)。

## CLI との対応

CLI は同じ結果型を `--json` で出力し、終了コードは 0 成功 / 1 失敗 / 2 引数エラー / 3 未認証 / 4 サービス到達不能。
