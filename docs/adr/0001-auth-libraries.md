# ADR-0001 認証基盤のライブラリと loopback 規約

* 状態: 採用 (2026-09-12)
* 対応: 全体計画 M0「認証基盤」、要件 FR-024〜045

## 決定

* パスワードは `argon2-cffi` (Argon2id)、パスキーは `py_webauthn` を使い、暗号・検証器を独自実装しない。
* OAuth 2.0 Authorization Code + PKCE (S256) はサーバー側を FastAPI 上に実装する。
  ネイティブは public client、`client_secret` を持たない。redirect URI は
  `http://127.0.0.1:<port>/callback` (RFC 8252 loopback、port は任意) を完全一致 (port 以外) で検証する。
* アクセストークンは 15 分の opaque トークン (SHA-256 hash 保存)、リフレッシュトークンは 30 日で
  使用ごとにローテーションし、旧トークン再使用で系列 (family) を失効する。
* localhost 配置は HTTP loopback (127.0.0.1 bind) とし、WebAuthn の RP ID は `localhost`。
  ブラウザーは `http://localhost` を secure context と扱う。リモート配置は HTTPS 必須で
  RP ID を配置 host に設定する。証明書検証の無効化オプションは設けない。

## 理由

要件は暗号実装の自作を避け、既存ライブラリの採用評価を M0 とした。opaque トークンは失効が
即時で、JWT 署名鍵の管理を MVP から外せる。

## 影響

クライアント (ClientCore) は redirect URI の port を実行時に確保し、ログイン中だけ待ち受ける。
