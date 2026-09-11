# ADR-0007 モノレポ構成と独立パッケージ

* 状態: 採用 (2026-09-12)
* 対応: 全体計画 4 節

## 決定

実装は 1 つのモノレポ。Python の API・各 worker は別 `pyproject.toml`・別 `uv.lock`・別 Dockerfile。
共有は `packages/python/audio_minutes_contracts` (schema の写し、Queue/ArtifactStore アダプター) と
`tests/fixtures/` まで。Swift は 1 つの Package (`packages/swift`) に ClientCore / ImportCore / RecorderCore を
ライブラリ target として置き、`apps/cli` と `apps/macos` が依存する。Chrome 拡張は npm 単独パッケージ。

## 理由

複数コンポーネントの同時変更を 1 PR で追え、contracts の変更を全消費者の試験で検出できる。
lockfile とイメージを分けることで API に推論依存が混入しない。
