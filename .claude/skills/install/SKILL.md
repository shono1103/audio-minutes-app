---
name: install
description: audio-minutes の導入前診断、配置 profile 選択、インストール、更新、復旧を安全に進める。導入・再導入・doctor・service・backup/restore の依頼で使う。
---

# install

audio-minutes のリポジトリルートで実行する。最初に読み取り専用の
`./scripts/inspect-host.sh --json` を実行し、OS、CPU、メモリ、GPU、空き容量、Colima、Docker、
必要ツール、対応アプリを確認する。検査結果にユーザー名、ホームディレクトリ、機器識別子、
資格情報の値を含めない。認証関連の環境変数は存在有無だけを扱う。

## profile の選択

- 指定がなければ macOS は `macos-colima-cpu`。CPU profile で GPU、Metal、MLX を有効化しない。
- `linux-cpu` は利用者が Linux 配置を指定した場合だけ選ぶ。
- `macos-colima-krunkit-vulkan` は利用者が GPU 評価を明示し、既存 VM と別 profile の作成を許可した場合だけ選ぶ。既存 profile を変更・再作成しない。
- ホスト資源を見て不足や競合を具体的に報告する。Colima の導入・起動、Docker context 切替、モデル取得、既存設定への変更は、実行直前に対象と影響を示して許可を得る。

## 導入と運用

実行前に次がそろっていることを確認する。

- `deploy/compose.yml` と選択 profile の設定例
- `deploy/settings.schema.json`
- API と両 worker の lockfile
- `scripts/doctor.sh`、`scripts/install.sh`、`scripts/service.sh`

一つでも欠けていれば独自の設定や代替インストーラーを作らず、未実装項目として停止する。
そろっている場合は `./scripts/doctor.sh` で事前検査し、許可後に
ルートの `./install.sh --profile <profile>` を実行する。再実行で既存の設定、録音、成果物、
Claude 資格情報を上書きしない。

MVP の cold install 対象は macOS 14.2以降。Homebrew と Xcode 15.3以降／Swift 5.10 は
本人が事前導入する。Homebrew が既にある macOS では、installer が管理対象5 formula (`docker`、`docker-compose`、
`colima`、`jq`、`node`) の不足分だけを提示し、利用者の同意後に導入する。`--yes` はこの同意を
含む。Xcode/Swift等のOS操作とLinux package managerは自動化せず、構造化 `PENDING` の内容を
利用者へ案内する。Colima起動の前後で global Docker context が同じことを確認する。

CPU profile は、取得時だけ外向き接続を許可して固定 revision のモデルを models volume へ保存し、
manifest の layout・size・SHA-256 と offline load/synthetic-audio smoke を通してから worker を起動する。
中断時は同じ `./install.sh` で再開する。業務 readiness は `./scripts/service.sh readiness`、モデルだけの
再診断は `./scripts/service.sh models {status|verify|smoke}` を使う。

運用操作は `./scripts/service.sh` に集約する。GUI に Docker socket を渡さない。
Claude のログインは自動化せず、minutes-worker の状態と CLI 互換性だけを検査して owner に案内する。
Claude 資格情報 volume は minutes-worker だけへ mount されていることを Compose 設定で確認する。

更新前は backup と DB・契約版の互換性を確認する。restore は復元先と上書き範囲を示して許可を得る。
`scripts/update.sh` は旧imageを退避し、model verify/offline smoke、client build、readinessまでを
installer共通処理で検査する。backup完了後は共有maintenance markerにより通常writeを止め、readiness成功時の
marker削除をcommit pointにする。失敗時は停止を維持して承認済みbackupと旧imageへrollbackし、完了後に旧サービスを再開する。
同じ変更を結果未確認のまま再送しない。

## 完了確認

少なくとも loopback 限定 API、各サービス状態、永続 volume、CPU backend、既知の合成音声、
再実行時の既存データ保持を確認する。GPU profile は GPU 実利用と CPU profile 非回帰を別々に記録する。
実機で確認できない録音権限、Chrome 接続、Claude ログイン、初回owner登録は `PENDING` として明示する。
