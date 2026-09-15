# audio-minutes Meet 連携拡張 (Chrome, MV3)

Google Chrome 上の **Google Meet (`meet.google.com`) タブだけ** を一覧し、利用者がこの拡張のアイコンから
明示的に許可したタブの音声を `chrome.tabCapture` で取得して、同じ Mac 上の audio-minutes GUI
(native messaging host `dev.audio_minutes.bridge`) へ PCM16 / 16 kHz / mono の 200 ms chunk として渡す。
音声・タブ情報を外部ホストへ送らず、ページの完全な URL はどのメッセージにも含めない。

## 動作の要点

* タブ一覧は host が `meet.google.com` の https タブに限定 (`src/tabs.ts`)。タイトル・ファビコン・音声再生状態だけを渡す。
* 録音の開始には対象タブ上での拡張アイコン操作 (popup の「Chrome で録音を許可」) が必要。GUI からの
  `request_capture` は対象タブを前面に出して待つだけで、勝手に開始しない。
* offscreen document で `AudioContext.destination` に接続し、録音中も利用者向け再生を維持する。
* 同じタブ内のページ遷移では継続し、タブが閉じられたら `capture_stopped (tab_closed)`。GUI 切断時は
  `host_disconnected` で停止する。host からの `ack` が 50 chunk (10 秒) 以上返らなければ `backpressure` で停止する。
* メッセージ契約は `src/messages.ts` (版 1)。NativeBridge 側 (`apps/macos/NativeBridge`) と同じ型を使う。

## 固定 ID (開発版)

`manifest.json` の `key` に開発用公開鍵を埋めているため、unpacked で読み込んでも ID は常に次になる。

```
cglcpocpendfgbhepidgbpilokapdlnm
```

native messaging host の manifest (`dev.audio_minutes.bridge.json`) の `allowed_origins` には
`chrome-extension:///` を明示する (ワイルドカードを使わない)。安定版を Chrome Web Store (Unlisted) へ
公開する場合は別 ID になるため、両 ID を並記して更新する。開発用秘密鍵 (`keys/*.pem`) は Git 管理外で、
unpacked 読み込みには不要。

## 開発者モードでの導入

```sh
cd extensions/chrome
npm ci
npm test
npm run build      # dist/ を生成
```

1. Chrome で `chrome://extensions` を開き、右上の「デベロッパー モード」を有効にする。
2. 「パッケージ化されていない拡張機能を読み込む」で `extensions/chrome/dist` を選ぶ。
3. 表示された ID が上の固定 ID と一致することを確認する。
4. audio-minutes GUI (または `scripts/install.sh`) が native messaging host manifest を
   `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/dev.audio_minutes.bridge.json` に置く。
5. GUI の録音対象ピッカーで Chrome 行が「連携済み」になり、Meet タブを展開できる。

## Unlisted 配布時の注意

* Chrome Web Store の審査・ポリシー要件は公開拡張と同じ。権限は `tabCapture`, `tabs`, `nativeMessaging`,
  `offscreen`, `storage` と `https://meet.google.com/*` に限定している。
* Store 版は ID が変わる。native host の `allowed_origins` へ Store 版 ID を追加し、開発版と併記する。
* `manifest.json` の `key` は Store 提出時に削除する (Store 側が鍵を管理する)。

## ディレクトリ

```
manifest.json     権限・固定 key・offscreen/popup 宣言
src/messages.ts   host との契約 (版 1)
src/tabs.ts       Meet タブ抽出 (URL 非送信)
src/pcm.ts        mixdown / リサンプル / PCM16 / chunk / 連番 / backpressure
src/background.ts service worker (native port、capture 制御)
src/offscreen.ts  tabCapture → AudioWorklet → chunk 送出、再生維持
src/worklet.ts    AudioWorkletProcessor (mono tap)
src/popup.*       状態表示と明示操作
test/             vitest (フィルタ・PCM・backpressure)
```
