# CLAUDE.md

audio-minutes の実装リポジトリ。要件・計画・タスク・Gherkin 手順書の正本は管理リポジトリ
(`audio-minutes`) にあり、ここには実装・契約・デプロイ・ADR・install スキルを置く。

* 全体構成と各ディレクトリの責務は [README.md](README.md)、設計判断は [docs/adr/](docs/adr/)。
* コンポーネント間の契約は [contracts/](contracts/) が正。schema を変えるときは版を上げ、
  `contracts/fixtures/` と `tests/contract/` を同時に更新する。
* API・各 worker は別パッケージ・別 lockfile・別イメージ。worker 間の直接呼出し、
  API の ORM・業務ロジックの worker への import は禁止 (詳細は README「疎結合の規則」)。
* 会議音声・文字起こし・議事録・Claude 資格情報は Git に入れない。テスト用音声は
  `tests/fixtures/audio/` の合成音声だけを使う。
* 導入・更新・診断は [.claude/skills/install/SKILL.md](.claude/skills/install/SKILL.md) (`/install`)。
  Codex からは `.agents/skills` のリンク経由で `$install`。
* ドキュメント・コメント・コミットメッセージは日本語で書く。
