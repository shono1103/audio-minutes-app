# audio-minutes-contracts

`contracts/schemas/*.v1.schema.json` の Python 側の写し (pydantic) と、API・worker が共有する
薄いアダプター (`JobQueue` / `ArtifactStore`) を提供する。API の ORM や業務ロジック、
Whisper・Claude の依存はここに置かない。

* `models` — RecordingPackage / Job / WorkerResult / Transcript / MinutesVersion / FormatProfile / Session / ApiError
* `schemas` — JSON Schema の読み込みと `validate(name, document)`
* `queue` — `JobQueue` Protocol と `PostgresJobQueue` (claim / heartbeat / complete / fail / cancel 確認)
* `artifacts` — `ArtifactStore` Protocol と `LocalArtifactStore` (tmp → 検証 → 不変 ID で公開)
* `ids` — artifact ID / job ID の生成
