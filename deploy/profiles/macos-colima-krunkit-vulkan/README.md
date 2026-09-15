# macos-colima-krunkit-vulkan

GPU 計画 (G1〜G5) の検証・配置 profile。**明示指定したときだけ**使い、既存の Colima `default` (vz) は
VM type・volume・Docker context を含めて一切変更しない。採用判定 (G6) 前に本番データを移さない。

## 前提

* Colima 0.10.0 以降 (krunkit 対応)、Lima の krunkit は実験的扱い
* macOS 上に `krunkit` (Homebrew `slp/krunkit/krunkit`) と Vulkan/Venus 対応の Mesa を持つゲストイメージ
* イメージは linux/arm64 の独自ビルド (`services/transcription-worker/Dockerfile.vulkan`)

## 手順案 (install スキルが対話で確認してから実行する)

```sh
# 1. 既存 default に触らず、評価用 profile を別 VM として作る (資源は 4 CPU / 8 GiB を初期比較条件にする)
colima start --profile audio-minutes-gpu-eval --vm-type krunkit --gpu --cpu 4 --memory 8 --disk 60

# 2. 以後のすべての操作で profile / context を明示する (グローバルな context 切替を前提にしない)
export DOCKER_CONTEXT=colima-audio-minutes-gpu-eval

# 3. ゲストとコンテナの双方で実 GPU を識別できることを確認する (llvmpipe/lavapipe なら不合格)
colima ssh --profile audio-minutes-gpu-eval -- ls -l /dev/dri
docker run --rm --device /dev/dri:/dev/dri audio-minutes/transcription-worker-vulkan:0.1.0 vulkaninfo --summary

# 4. .env を生成し、gpu profile で起動する (ポート 8797、project 名は同じだが別 VM なので衝突しない)
./scripts/install.sh --profile macos-colima-krunkit-vulkan
```

## 判定の注意

* `vulkaninfo` に物理デバイスが出るだけで GPU 成功にしない。transcript の `processing.backend.gpu_verified` と
  ホスト側 GPU 活動を照合する (ADR-0006)。
* GPU 初期化・モデル互換・演算検証に失敗した場合は `gpu_unavailable` などで停止し、CPU 結果を GPU 結果として返さない。
  評価 run では `AM_GPU_CPU_FALLBACK=0` を維持する。
* `/dev/dri` の render device だけを渡し、`privileged` は使わない。必要なデバイスが増える場合は理由を ADR に残す。
* CPU 配置へ戻す場合は同時書込みを止め、DB + artifacts backup を経由して移す。URL を戻すだけで復旧したと扱わない。
