# contracts — コンポーネント間の版付き契約

API・transcription-worker・minutes-worker・macOS クライアント・Chrome 拡張が共有する契約の正本。
実装ライブラリ (ORM・faster-whisper・Claude CLI) の型をここへ持ち込まない。

| ファイル | 内容 |
| --- | --- |
| `schemas/*.v1.schema.json` | JSON Schema (draft 2020-12)。`schema_version` は `"<name>/1"` |
| `fixtures/` | 各 schema に適合する代表例。`tests/contract/` が検証する |
| `api/openapi.json` | minutes-api が生成する OpenAPI。CI でこのファイルとの差分を検出する |
| `api/README.md` | エンドポイント一覧・認可・エラー・ポーリング規約 |
| `internal/claude-control.md` | minutes-api → minutes-worker の Claude 認証制御契約 (内部ネットワーク限定) |
| `sql/jobs.sql` | 永続ジョブテーブルと worker 用ロールの DDL。API の migration から参照する |

## 不変条件

* 時刻は録音開始基準の整数ミリ秒。`sample_index = round(ms * sample_rate / 1000)`。
* 録音由来の 2 トラックは音源の区別 (`app` / `microphone`) であり話者の識別ではない。
  取り込みは `mixed` 単一トラック。同時発話は時刻順で保持し、別トラックの同時刻を重複として削除しない。
* segment 時刻は必須、単語時刻は optional。未取得の単語時刻・言語確率を生成しない。
* job は少なくとも一回配送され得る。lease・attempt・`revision` で古い attempt の完了を拒否する。
* worker は成果物を一時ファイルへ書き、検証後に不変 artifact ID で公開する。DB の現在版 pointer は API だけが更新する。
* エラー・失敗には秘密・本文・ローカルパスを含めない。
* schema を破壊的に変えるときは `v2` を追加し、旧版の fixture を残して互換試験を通す。
