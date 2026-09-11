# アーキテクチャ概要

```text
macOS ホスト
  SwiftUI GUI (apps/macos) ─ RecorderCore / ImportCore / ClientCore (packages/swift)
       │       └ Chrome 拡張 (extensions/chrome) ⇄ NativeBridge (apps/macos/NativeBridge、ローカル限定)
       └ audio-minutes CLI (apps/cli) ─ 同じ ClientCore ──┐  HTTP / tus (loopback)
                                                          ▼
Colima / Docker Compose (deploy/)
  127.0.0.1:8787 → minutes-api (services/minutes-api) → PostgreSQL・jobs・artifacts volume
                       │                                   ▲                 ▲
                       │ 内部 Claude 制御契約     transcription-worker   minutes-worker
                       └────────────────────────▶ (faster-whisper CPU /  (Claude CLI +
                                                   whisper.cpp Vulkan)    専用資格情報 volume)
```

* **契約が境界**: `contracts/` の JSON Schema と `contracts/sql/jobs.sql` だけを共有する。
  Python 側の写しは `packages/python/audio_minutes_contracts` (pydantic + Queue/ArtifactStore アダプター)。
* **API は推論依存を持たない**: Whisper・Claude CLI は worker イメージだけに入る。
* **ジョブは PostgreSQL の jobs テーブル**: 短い transaction で `FOR UPDATE SKIP LOCKED` により lease、
  heartbeat、attempt 上限、backoff、`revision` による古い attempt の完了拒否を行う。
* **reconciler**: API プロセス内の background task が `succeeded` ジョブを取り込み、
  成果物確定 (transcripts / minutes_versions / 現在版 pointer) と次工程投入を一つの transaction で行う。
* **成果物は artifact ID で参照**: 同一ホストでは `artifacts` volume を API と worker で共有し、
  `LocalArtifactStore` が `tmp/<id>.part` → 検証 → `<id[:2]>/<id>` へ rename する。
* **Claude 認証**: URL は minutes-worker のメモリだけに置き、API が owner へ中継する。
  詳細は `contracts/internal/claude-control.md`。
* **GPU**: `transcription-worker` の `WhisperCppAdapter` に `requested_backend=vulkan` を渡す別 profile。
  CPU 既定を変えず、GPU 初期化失敗は `gpu_unavailable` で停止し CPU 結果を GPU 結果として返さない。
