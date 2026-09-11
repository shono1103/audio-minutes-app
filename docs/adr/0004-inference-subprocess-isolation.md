# ADR-0004 推論を子プロセスへ隔離する

* 状態: 採用 (2026-09-12)
* 対応: 全体計画 2 節 (終了コード 139 の切り分け)、GPU 計画 4 節

## 決定

transcription-worker はジョブごとに engine を子プロセス (`multiprocessing`、spawn) で実行し、
親プロセスは heartbeat・timeout・cancel・終了コード監視だけを行う。子プロセスの異常終了
(139 など) は `engine_crashed` として attempt を失敗させ、worker 本体と API は生き残る。

whisper.cpp アダプターは CLI 起動方式を初期実装とし、JSON 出力と stderr の `system_info` /
backend 表示から `effective_backend` を判定する。常駐 IPC 方式は交換可能な `EngineRunner` 境界で追加する。

## 影響

モデルは原則 1 つ常駐。routed 戦略で 2 モデルを保持する場合はメモリ上限を確認し、
不足時は順次ロード (unload → load) に切り替える。
