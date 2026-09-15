# 固定イメージ情報

導入時に `scripts/install.sh` が build したイメージの digest を追記する。手で編集しない。
digest は端末ごとに異なるため、この表は「その端末で何を導入したか」の記録であり、配布物の正本ではない。
配布物の版は各サービスの `uv.lock` と `Dockerfile` の `ARG` で固定する。

| 記録日時 (UTC) | profile | image | digest |
| --- | --- | --- | --- |
