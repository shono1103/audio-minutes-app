# ADR-0002 tus を minutes-api 内に実装する

* 状態: 採用 (2026-09-12)
* 対応: 全体計画 M0「upload/ジョブ」、要件 FR-046〜051

## 決定

tusd を別コンテナで置かず、tus 1.0 の Core / Creation / Expiration / Termination を
minutes-api のルーター (`/v1/sessions/{id}/uploads`, `/v1/uploads/{upload_id}`) として実装する。

## 理由

計画は tusd を候補としつつ「hook だけで全リクエストの認可を満たすとは扱わない」と定めた。
API 内実装なら POST/HEAD/PATCH/DELETE の全操作で同じ Bearer 認可・所有者検査を通せ、
公開ポートも増えない。MVP の同時 upload は 2 本で、tusd の並列性能は不要。

## 影響

* `Upload-Metadata` は `track_id` の照合だけに使い、ファイル名やパスに使わない。
* 受信は `uploads` volume の `<upload_id>.part` に追記し、offset はサーバー DB が正。
* Checksum 拡張は採用せず、finalize 時の全体 SHA-256 照合で整合性を確認する。
