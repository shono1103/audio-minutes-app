# ADR-0008 localhost は loopback HTTP、リモートは TLS リバースプロキシ

* 状態: 採用 (2026-09-12)
* 対応: 要件 7 節「プライバシーと安全性」、全体計画 M0「認証基盤」

## 決定

`macos-colima-cpu` では minutes-api を `127.0.0.1:8787` だけへ publish し TLS を使わない。
自己署名 CA を macOS の信頼ストアへ入れる OS 設定変更を MVP の導入手順から外すため。
`linux-cpu` (将来) は Caddy をプロキシとして `deploy/profiles/linux-cpu/` に置き、
API・認証画面だけを 443 で公開する。DB・worker・内部制御 API はどの profile でも publish しない。
クライアントは `http://127.0.0.1` / `http://localhost` 以外の平文 URL を拒否する。
