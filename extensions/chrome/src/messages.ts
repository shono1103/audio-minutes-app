/**
 * Chrome 拡張と macOS 側 NativeBridge (apps/macos/NativeBridge) の間のメッセージ契約。
 * 版は `PROTOCOL_VERSION`。互換性のない変更では版を上げ、双方で `hello` の版照合により拒否する。
 *
 * 音声はローカルの native messaging host にだけ渡し、外部ホストへは送らない。
 * タブの完全な URL はどのメッセージにも含めない (host は常に meet.google.com)。
 */

export const PROTOCOL_VERSION = 1 as const;
export const NATIVE_HOST_NAME = "dev.audio_minutes.bridge";
export const EXTENSION_VERSION = "0.1.0";

/** 拡張 → 音声形式の固定値 */
export const AUDIO_SAMPLE_RATE = 16000;
export const AUDIO_CHANNELS = 1;
export const CHUNK_MS = 200;
export const SAMPLES_PER_CHUNK = (AUDIO_SAMPLE_RATE * CHUNK_MS) / 1000; // 3200
/** ack が返らない chunk がこの数を超えたら停止する (200 ms × 50 = 10 秒) */
export const MAX_PENDING_CHUNKS = 50;

export interface TabSummary {
  tab_id: number;
  title: string;
  /** https のファビコン URL だけ。ページ URL は含めない */
  favicon_url: string | null;
  audible: boolean;
  window_id: number;
}

export type CaptureStopReason =
  | "user"
  | "tab_closed"
  | "host_disconnected"
  | "backpressure"
  | "stream_ended"
  | "error";

// --- 拡張 → host --------------------------------------------------------------

export interface HelloMessage {
  type: "hello";
  version: typeof PROTOCOL_VERSION;
  extension_version: string;
  extension_id: string;
}

export interface TabsMessage {
  type: "tabs";
  version: typeof PROTOCOL_VERSION;
  tabs: TabSummary[];
}

export interface CapturePendingMessage {
  type: "capture_pending";
  version: typeof PROTOCOL_VERSION;
  capture_id: string;
  tab_id: number;
}

export interface CaptureStartedMessage {
  type: "capture_started";
  version: typeof PROTOCOL_VERSION;
  capture_id: string;
  tab_id: number;
  sample_rate: typeof AUDIO_SAMPLE_RATE;
  channels: typeof AUDIO_CHANNELS;
  /** 拡張側の壁時計 (epoch ms)。GUI は自身の共通時刻基準へ写像する */
  started_at_epoch_ms: number;
}

export interface ChunkMessage {
  type: "chunk";
  version: typeof PROTOCOL_VERSION;
  capture_id: string;
  /** 0 から始まる連番。欠落検出用 */
  seq: number;
  /** capture_started からの経過ミリ秒 (サンプル数から算出) */
  pts_ms: number;
  /** PCM16LE mono 16 kHz を base64 化したもの */
  data: string;
  sample_count: number;
}

export interface CaptureStoppedMessage {
  type: "capture_stopped";
  version: typeof PROTOCOL_VERSION;
  capture_id: string;
  reason: CaptureStopReason;
  /** 最後に送った seq。未開始なら -1 */
  last_seq: number;
  detail?: string;
}

export interface ErrorMessage {
  type: "error";
  version: typeof PROTOCOL_VERSION;
  code: "capture_failed" | "tab_not_meet" | "no_pending_request" | "internal";
  message: string;
  capture_id?: string;
}

export type ExtensionToHostMessage =
  | HelloMessage
  | TabsMessage
  | CapturePendingMessage
  | CaptureStartedMessage
  | ChunkMessage
  | CaptureStoppedMessage
  | ErrorMessage;

// --- host → 拡張 --------------------------------------------------------------

export interface HelloAckMessage {
  type: "hello_ack";
  version: number;
  host_version: string;
  compatible: boolean;
}

export interface ListTabsMessage {
  type: "list_tabs";
  version: number;
}

/** GUI が対象タブを選んだ。拡張は該当タブを前面にし、利用者の明示操作 (popup) を待つ */
export interface RequestCaptureMessage {
  type: "request_capture";
  version: number;
  capture_id: string;
  tab_id: number;
}

export interface StopCaptureMessage {
  type: "stop_capture";
  version: number;
  capture_id: string;
}

/** 受信済み chunk の確認。backpressure 判定に使う */
export interface AckMessage {
  type: "ack";
  version: number;
  capture_id: string;
  seq: number;
}

export type HostToExtensionMessage =
  | HelloAckMessage
  | ListTabsMessage
  | RequestCaptureMessage
  | StopCaptureMessage
  | AckMessage;

// --- 拡張内部 (background ⇄ offscreen ⇄ popup) ------------------------------

export interface StartOffscreenCapture {
  kind: "offscreen:start";
  capture_id: string;
  tab_id: number;
  stream_id: string;
}

export interface StopOffscreenCapture {
  kind: "offscreen:stop";
  capture_id: string;
  reason: CaptureStopReason;
}

export interface OffscreenStarted {
  kind: "offscreen:started";
  capture_id: string;
  started_at_epoch_ms: number;
}

export interface OffscreenChunk {
  kind: "offscreen:chunk";
  capture_id: string;
  seq: number;
  pts_ms: number;
  data: string;
  sample_count: number;
}

export interface OffscreenStopped {
  kind: "offscreen:stopped";
  capture_id: string;
  reason: CaptureStopReason;
  last_seq: number;
  detail?: string;
}

export interface PopupGetState {
  kind: "popup:get_state";
}

export interface OffscreenGetState {
  kind: "offscreen:get_state";
}

export interface PopupStartCapture {
  kind: "popup:start_capture";
  tab_id: number;
}

export interface PopupStopCapture {
  kind: "popup:stop_capture";
}

export type ConnectionState = "disconnected" | "connecting" | "connected" | "incompatible";

export interface ExtensionState {
  connection: ConnectionState;
  host_version: string | null;
  pending_capture: { capture_id: string; tab_id: number } | null;
  active_capture: { capture_id: string; tab_id: number; started_at_epoch_ms: number; last_seq: number } | null;
}

export type InternalMessage =
  | OffscreenGetState
  | StartOffscreenCapture
  | StopOffscreenCapture
  | OffscreenStarted
  | OffscreenChunk
  | OffscreenStopped
  | PopupGetState
  | PopupStartCapture
  | PopupStopCapture;

export function isHostMessage(value: unknown): value is HostToExtensionMessage {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return typeof record.type === "string" && typeof record.version === "number";
}
