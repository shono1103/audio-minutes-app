# linux-cpu (将来・MVP cold install 対象外)

Ubuntu 22.04 LTS 以降 / x86_64 / Docker Engine + Compose を基準候補とする Linux リモート配置の雛形。
MVP では実配置・運用検証を行わない (要件 FR-067 は「将来」)。`install.sh` が成功しても
macOS client・TLS reverse proxy・リモート Claude subscription の利用可否までは構築・保証しない。

* `Caddyfile.example` を `/etc/caddy/Caddyfile` に置き、443 だけを外部へ公開する。
* `.env.example` から `.env` を作り `AM_PUBLIC_BASE_URL` / `AM_RP_ID` を配置 host に合わせる。
* `scripts/install.sh --profile linux-cpu` は Colima 検査を飛ばし、Docker Engine と Compose を検査する。
  不足パッケージがあっても apt/dnf等は自動実行せず、`PENDING code=package_manager_required` で停止する。
* Claude subscription 認証のリモート利用は、実装時点の Anthropic 利用条件を再確認してから有効化する。
