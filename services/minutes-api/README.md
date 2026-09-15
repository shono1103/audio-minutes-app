# minutes-api

audio-minutes の認証・セッション・tus upload・ジョブ投入・成果物・議事録版を所有する FastAPI サービス。
Whisper と Claude CLI はこのパッケージへ依存させず、版付き契約、PostgreSQL のジョブ、artifact ID だけで worker と連携する。

```sh
uv sync --extra dev
uv run pytest
uv run minutes-api
```

初回 owner 用 URL は DB migration 後に `uv run minutes-api-admin bootstrap` で発行する。
初回 owner・招待ユーザーはパスキーだけで登録でき、パスワードは利用できない環境向けのfallbackとして任意。
既存ユーザーの追加は `POST /v1/account/passkeys/browser` が返す一回限りURLを開き、server画面内で
本人再認証とWebAuthn登録を完了する。nativeクライアントはパスワードやWebAuthn payloadを扱わない。

bootstrap・招待・再認証・パスキー登録の一回限り token は URL path に含まれるため、Uvicorn の
生 access log はすべてのサポート対象起動経路で無効にする。Docker の stdout archive に request
target や query を保存しない。WebSocketは使用しないため配置時は無効化し、直接Uvicorn起動時も
handshakeのrequest-bearing logだけを除く。起動・停止ログと、秘密を含めない
application／audit／一般エラーログは維持する。
