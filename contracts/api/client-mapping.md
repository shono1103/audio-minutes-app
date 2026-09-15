# API route と Swift クライアントの対応表

`minutes-api` の各 route が返す JSON と、`packages/swift/Sources/ClientCore/SessionService.swift`
のメソッド・戻り値型の対応。**一覧は配列ではなく `{"items": [...]}` の envelope** なので、
配列を直接 decode すると空一覧でも必ず失敗する。ここが正本で、
`contracts/fixtures/api/` の fixture と `ApiResponseDecodingTests` が機械検証する。

応答本文の要素型は `contracts/schemas/*.schema.json` が正。この表は envelope と
「何を返すか」の対応だけを示す。

## 認証・アカウント

| route | 応答 | Swift メソッド | 戻り値 |
| --- | --- | --- | --- |
| `GET /v1/health` | `{status, version}` | `health()` | `Health` |
| `GET /v1/capabilities` | `{version, contracts, database, workers[], transcription_available, minutes_available, claude, limits, poll_interval_ms}` | `capabilities()` | `Capabilities` |
| `GET /v1/me` | `{user_id, email, role, reauth_valid_until, passkey_count, default_format_profile_id}` | `me()` | `Me` |
| `POST /v1/auth/reauth/browser` | `{request_id, status, reauth_url, expires_at, reauth_valid_until}` | `startBrowserReauth()` | `BrowserReauth` |
| `GET /v1/auth/reauth/browser/{request_id}` | `{request_id, status, expires_at, reauth_valid_until}` | `browserReauthStatus(_:)` | `BrowserReauth` |
| `POST /v1/auth/reauth` | `{reauth_valid_until}` | 旧形式 (nativeでは使用禁止) | なし |
| `POST /v1/account/recovery-codes` | `{recovery_codes: [...]}` | `regenerateRecoveryCodes()` | `RecoveryCodes` |
| `GET /v1/account/passkeys` | **`{items: [...]}`** | `passkeys()` | `[Passkey]` |
| `POST /v1/account/passkeys/browser` | `{request_id, status, passkey_url, expires_at}` | `startPasskeyRegistration()` | `PasskeyRegistration` |
| `GET /v1/account/passkeys/browser/{request_id}` | `{request_id, status, expires_at}` | `passkeyRegistrationStatus(_:)` | `PasskeyRegistration` |
| `DELETE /v1/account/passkeys/{id}` | 204 | `deletePasskey(_:)` | なし |

## セッション

| route | 応答 | Swift メソッド | 戻り値 |
| --- | --- | --- | --- |
| `POST /v1/sessions` | `session` | `createSession(_:)` | `Session` |
| `GET /v1/sessions` | **`{items: [...], next_cursor}`** | `listSessions(cursor:limit:)` | `SessionList` |
| `GET /v1/sessions/{id}` | `session` | `session(_:)` | `Session` |
| `PATCH /v1/sessions/{id}` | `session` | `update(_:_:)` | `Session` |
| `DELETE /v1/sessions/{id}` | 204 | `delete(_:)` | なし |
| `POST /v1/sessions/{id}/finalize` | `session` | `finalize(_:)` | `Session` |
| `POST /v1/sessions/{id}/retry` | **`{job_id, session}`** | `retry(_:stage:languageMode:)` | `AcceptedJob` |
| `GET /v1/sessions/{id}/jobs` | **`{items: [...]}`** | `jobs(_:)` | `[JobSummary]` |
| `POST /v1/sessions/{id}/jobs/{jid}/cancel` | 単一 job | `cancelJob(_:jobID:)` | `JobSummary` |
| `POST /v1/sessions/{id}/jobs/{job_id}/cancel` | **単一** job | `cancelJob(_:jobID:)` | `JobSummary` |
| `GET /v1/sessions/{id}/shares` | **`{items: [...]}`** | `shares(_:)` | `[ShareEntry]` |
| `POST /v1/sessions/{id}/shares` | **単一** `{user_id, email}` | `share(_:email:)` | `ShareEntry` |
| `DELETE /v1/sessions/{id}/shares/{uid}` | 204 | `unshare(_:userID:)` | なし |

`POST /retry` と `POST /minutes/regenerate` は `Idempotency-Key` ヘッダーに対応する。
同じ値での再送は同じ job を返し、指定しなければ要求ごとに新しい job を作る。

## 成果物・議事録

| route | 応答 | Swift メソッド | 戻り値 |
| --- | --- | --- | --- |
| `GET /v1/sessions/{id}/audio/{track_id}` | 音声 (Range) | `audio(_:trackID:range:)` | `RawResponse` |
| `GET /v1/sessions/{id}/transcript` | `transcript` | `transcript(_:)` | `Transcript` |
| `GET /v1/sessions/{id}/transcript.md` | text/markdown | `transcriptMarkdown(_:)` | `String` |
| `GET /v1/sessions/{id}/minutes` | `{version, body_markdown}` | `currentMinutes(_:)` | `MinutesDocument` |
| `GET /v1/sessions/{id}/minutes/versions` | **`{items: [...], current_minutes_version_id}`** | `minutesVersions(_:)` | `MinutesVersionList` |
| `GET /v1/sessions/{id}/minutes/versions/{vid}` | `{version, body_markdown}` | `minutesVersion(_:versionID:)` | `MinutesDocument` |
| `POST /v1/sessions/{id}/minutes/versions` | **`{version}`** | `saveManualEdit(_:_:)` | `MinutesVersion` |
| `POST /v1/sessions/{id}/minutes/regenerate` | **`{job_id, session}`** | `regenerate(_:_:)` | `AcceptedJob` |
| `POST /v1/sessions/{id}/minutes/current` | **`{version}`** (セッションではない) | `selectCurrent(_:versionID:)` | `MinutesVersion` |
| `POST /v1/sessions/{id}/minutes/versions/{vid}/restore` | **`{version}`** | `restore(_:versionID:)` | `MinutesVersion` |
| `GET /v1/sessions/{id}/minutes/compare` | `{from, to, diff}` | `compare(_:from:to:)` | `MinutesCompare` |

## フォーマット

| route | 応答 | Swift メソッド | 戻り値 |
| --- | --- | --- | --- |
| `GET /v1/formats` | **`{items: [...]}`** | `formats()` | `[FormatProfile]` |
| `GET /v1/formats/{id}` | `format-profile` | `format(_:)` | `FormatProfile` |
| `POST /v1/formats` | `format-profile` | `createFormat(_:)` | `FormatProfile` |
| `PUT /v1/formats/{id}` | `format-profile` | `updateFormat(_:_:)` | `FormatProfile` |
| `DELETE /v1/formats/{id}` | 204 | `deleteFormat(_:)` | なし |
| `POST /v1/formats/{id}/duplicate` | `format-profile` | `duplicateFormat(_:)` | `FormatProfile` |
| `POST /v1/formats/{id}/default` | `format-profile` | `setDefaultFormat(_:)` | `FormatProfile` |
| `POST /v1/formats/preview` | `{markdown}` | `previewFormat(_:)` | `FormatPreview` |

## 管理 (owner)

| route | 応答 | Swift メソッド | 戻り値 |
| --- | --- | --- | --- |
| `GET /v1/admin/invitations` | **`{items: [...]}`** (url なし) | `invitations()` | `[Invitation]` |
| `POST /v1/admin/invitations` | **単一** `{id, email, role, expires_at, url}` | `invite(email:role:)` | `Invitation` |
| `DELETE /v1/admin/invitations/{id}` | 204 | `revokeInvitation(_:)` | なし |
| `GET /v1/admin/users` | **`{items: [...]}`** | `users()` | `[AdminUser]` |
| `PATCH /v1/admin/users/{id}` | **単一** `{id, email, role, disabled}` (created_at なし) | `updateUser(_:_:)` | `AdminUser` |
| `GET /v1/admin/retention` | `{upload_hours, audio_days, log_days}` | `retention()` | `Retention` |
| `PUT /v1/admin/retention` | 同上 | `updateRetention(_:)` | `Retention` |
| `GET /v1/admin/audit` | **`{items: [...], next_cursor}`** | (未使用) | — |
| `GET /v1/admin/claude` | 接続状態の全項目 | `claudeStatus()` | `ClaudeStatus` |
| `POST /v1/admin/claude/login` | `{auth_session_id, expires_at, state}` | `claudeLogin()` | `ClaudeLoginSession` |
| `GET /v1/admin/claude/login/{id}` | `{auth_session_id, state, expires_at, url?, failure_code?}` | `claudeLoginStatus(_:)` | `ClaudeLoginSession` |
| `POST /v1/admin/claude/login/{id}/code` | `{auth_session_id, state}` | `submitClaudeLoginCode(_:code:)` | `ClaudeLoginSession` |
| `POST /v1/admin/claude/login/{id}/cancel` | `{auth_session_id, state}` | `claudeCancelLogin(_:)` | `ClaudeLoginSession` |
| `POST /v1/admin/claude/logout` | **`{state}` だけ** | `claudeLogout()` | `ClaudeStatus` (state 以外は nil) |

## 一覧 cursor の規約

`GET /v1/sessions` と `GET /v1/admin/audit` の `next_cursor` は **最後に返した行**を指す。
次ページはその行より後ろ (排他) を取る。返していない行を cursor にすると境界で 1 件飛ぶ。
