# ADR-0003 ジョブキューは PostgreSQL の jobs テーブル

* 状態: 採用 (2026-09-12)
* 対応: 全体計画 3 節「疎結合の実装規則」、FR-054〜057、FR-070

## 決定

Redis 等を追加せず、`contracts/sql/jobs.sql` の `jobs` / `job_events` を唯一のキューにする。
worker は `am_worker` ロールで接続し、`claim` (`FOR UPDATE SKIP LOCKED`)、`heartbeat`、`complete`、
`fail`、`cancel 確認` だけを行う。業務テーブル (sessions / transcripts / minutes_versions) は API 所有。

## 規則

* lease は短い transaction で取得し、推論中に DB ロックを保持しない。
* `revision` を更新ごとに +1 し、complete/fail は `attempt` と `lease_owner` と `revision` が一致する
  行だけを更新する。一致しない場合は `lease_lost` として結果を捨てる。
* lease 切れ (`lease_expires_at < now()`) の `leased`/`running` は再 claim 対象。
  `attempt >= max_attempts` なら `failed`。backoff は `available_at` で表す。
* `cancel_requested=true` の実行中jobはlease切れ後に別workerがfencing付きでterminal化する。
  minutesで外部送信開始済みなら取消完了と断定せず`outcome=unknown`、未送信なら`cancelled`とする。
* minutes-workerはClaude呼出しの直前ごとに`authorize_minutes_dispatch`を実行する。同関数がjobをlockし、
  cancel/lease/session削除/current user/current Claude owner/外部送信許可を同時に再検査する。
  無効ならjobを`cancelled`へ移し、外部送信しない。
* API 側 reconciler が `succeeded` を取り込み、成果物確定と次工程投入を一つの transaction で行う。
  `unknown` outcome (Claude 応答喪失) は自動再送せず利用者の明示再実行を待つ。
