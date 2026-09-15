# transcription-worker

audio-minutes の文字起こしワーカー。PostgreSQL の `jobs` テーブルから `transcription` ジョブを lease し、
faster-whisper CPU (INT8) または whisper.cpp (CPU / Vulkan) で文字起こしして `transcript.json` / `transcript.md` を
成果物ストアへ書く。API の ORM・業務テーブルには依存せず、`audio_minutes_contracts` の Queue / ArtifactStore だけを使う。

## 構成

```text
src/transcription_worker/
├── config.py            # 環境変数 (docs/integration-contract.md) の読み込み
├── audio.py             # ffmpeg による decode / 16 kHz mono 正規化と normalization メタデータ
├── vad.py               # Silero VAD による発話窓 (min_speech / min_silence / pad / 重なり)
├── resources.py         # CPU arch / cgroup メモリ上限 / peak RSS
├── engines/
│   ├── base.py          # TranscriptionEngine Protocol、ModelRef、RawSegment、Capabilities、例外
│   ├── faster_whisper_cpu.py  # FasterWhisperCpuAdapter (device=cpu、compute_type=int8)
│   ├── whisper_cpp.py         # WhisperCppAdapter (CLI 起動、stderr から実効 backend を判定)
│   └── manager.py       # ModelManager (常駐数の上限・順次ロード)
├── pipeline/
│   ├── review.py        # 要確認区間の検出 (音声あり出力なし / 疎)
│   ├── merge.py         # ms 変換、時間範囲の置換、2 track の時刻順 merge
│   ├── routing.py       # 言語判定の閾値・最小区間長・隣接窓の平滑化
│   ├── strategies.py    # vad_turbo / routed / whole_retry / fixed_ja / fixed_en / mixed
│   └── transcribe.py    # ジョブ入力 → Transcript (schema 検証込み)
├── models.py            # モデル事前取得 CLI (revision 固定、manifest)
├── runner.py            # queue loop、子プロセス隔離、heartbeat、失敗分類、CPU fallback
└── __main__.py
```

## 起動

```sh
uv sync
AM_WORKER_DATABASE_URL=postgresql://am_worker:...@db:5432/audio_minutes \
AM_ARTIFACTS_DIR=/var/lib/audio-minutes/artifacts \
AM_MODELS_DIR=/var/lib/audio-minutes/models \
uv run transcription-worker
```

環境変数の一覧と既定値は `docs/integration-contract.md` に従う。CPU profile (`AM_BACKEND=cpu`) では GPU を
初期化しない。`AM_BACKEND=vulkan` は `AM_ENGINE=whisper.cpp` のときだけ有効で、実 GPU を検出できなければ
`gpu_unavailable` で失敗し、CPU 結果を GPU 結果として返さない。

## モデルの事前取得

```sh
uv run python -m transcription_worker.models prefetch --models-dir /var/lib/audio-minutes/models
uv run python -m transcription_worker.models verify   --models-dir /var/lib/audio-minutes/models
HF_HUB_OFFLINE=1 uv run python -m transcription_worker.models smoke --models-dir /var/lib/audio-minutes/models
uv run python -m transcription_worker.models status   --models-dir /var/lib/audio-minutes/models --json
```

`manifest.json` に model_id / revision / 各ファイルの SHA-256 を記録する。取得中断は marker で検出し、
同じ prefetch で部分 cache から再開する。通常処理では `HF_HUB_OFFLINE=1` で動き、固定モデル両方の
offline load と合成PCM smoke が完了するまで worker は ready を通知しない。

## 戦略 (`AM_STRATEGY`、`language_mode=auto` のとき)

| 戦略 | 動作 | 位置づけ |
| --- | --- | --- |
| `vad_turbo` (既定) | 全 VAD 窓を多言語モデル (Turbo) で処理 | 保守的な比較基準 (B0) |
| `routed` | 窓ごとに言語判定。ja/en で確率 ≥ 0.85 かつ ≥ 3 秒だけ専門モデルへ、他は Turbo | 日本語精度候補 (C) |
| `whole_retry` | Turbo で全体処理 → VAD 窓と照合 → 要確認区間だけ 1 回再処理し、時間範囲ごとに置換 | 速度候補 (E) |

`ja` / `en` は専門モデル固定、`mixed` は VAD 窓ごとに Turbo を言語未指定で使い専門モデルへ分割しない。
2 トラックはトラック別に処理し、`start_offset_ms` を加算して共通時刻へ戻し、`source` を付けて時刻順に merge する。
重なりは削除しない。要確認 (FR-154/155) は `review` に理由・候補・採否理由を残し、1 区間 1 回で再試行を止める。

## テスト

```sh
uv run pytest            # モデル不要の単体テスト
uv run pytest -m slow    # 実モデル smoke (AM_MODELS_DIR に cache が必要)
uv run pytest -m integration  # Docker 上の PostgreSQL を使う queue 統合
```

## Docker

```sh
docker build -f services/transcription-worker/Dockerfile -t audio-minutes/transcription-worker .
docker build -f services/transcription-worker/Dockerfile.vulkan --platform linux/arm64 -t audio-minutes/transcription-worker-vulkan .
```

build context はリポジトリルート。`Dockerfile.vulkan` は GPU 計画 G2 の対象で、この時点ではビルド・実行未検証。
