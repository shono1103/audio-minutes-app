# ADR-0006 GPU backend の要求・実効・検証をメタデータに残す

* 状態: 採用 (2026-09-12)
* 対応: GPU 計画 G0 / 5 節、FR-058 / 071 の拡張

## 決定

transcript の `processing.backend` に `requested_backend`、`effective_backend`、`gpu_verified`、
`gpu_name`、`driver`、`vm`、`fallback_reason`、`software_renderer_detected` を持つ (optional 追加、schema v1 のまま)。

* CPU profile (`macos-colima-cpu`, `linux-cpu`) は `requested_backend=cpu` で GPU を初期化しない。
* `macos-colima-krunkit-vulkan` は `requested_backend=vulkan`。`vulkaninfo` の物理デバイスと
  whisper.cpp の選択 device を照合し、llvmpipe/lavapipe を検出したら `gpu_verified=false`。
* GPU 要求で初期化・モデル互換・演算検証に失敗したら `gpu_unavailable` / `unsupported_model` /
  `backend_error` で失敗させ、CPU 実行を GPU 結果として返さない。
  運用の CPU 再試行は明示設定 (`AM_GPU_CPU_FALLBACK=1`) のときだけ、別 attempt として最大 1 回。
