/**
 * service worker: NativeBridge との native messaging、Meet タブ一覧、capture の開始・停止・backpressure を担当する。
 * タブの完全な URL・音声を外部へ送らない。capture の開始は利用者の明示操作 (popup) が必要。
 */
import {
  EXTENSION_VERSION,
  MAX_PENDING_CHUNKS,
  NATIVE_HOST_NAME,
  PROTOCOL_VERSION,
  AUDIO_CHANNELS,
  AUDIO_SAMPLE_RATE,
  isHostMessage,
  type CaptureStopReason,
  type ExtensionState,
  type ExtensionToHostMessage,
  type HostToExtensionMessage,
  type InternalMessage,
} from "./messages";
import { Backpressure } from "./pcm";
import { isMeetUrl, summarizeMeetTabs } from "./tabs";
import { restoredCaptureMatches } from "./capture-state";

const OFFSCREEN_URL = "offscreen.html";
const RECONNECT_MIN_MS = 2000;
const RECONNECT_MAX_MS = 30000;

const state: ExtensionState = {
  connection: "disconnected",
  host_version: null,
  pending_capture: null,
  active_capture: null,
};

let port: chrome.runtime.Port | null = null;
let reconnectDelay = RECONNECT_MIN_MS;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
const backpressure = new Backpressure(MAX_PENDING_CHUNKS);
let tabsDebounce: ReturnType<typeof setTimeout> | null = null;
let restoredCaptureLost: ExtensionState["active_capture"] = null;

// --- 状態の永続化 (service worker の再起動に備える) --------------------------

async function persistState(): Promise<void> {
  await chrome.storage.session.set({ state });
}

async function restoreState(): Promise<void> {
  const stored = await chrome.storage.session.get("state");
  if (stored.state) Object.assign(state, stored.state as ExtensionState);
  // 再起動後は接続もポートも失っているので接続状態だけ初期化する
  state.connection = "disconnected";
  state.host_version = null;
  if (state.active_capture) {
    const contexts = await chrome.runtime.getContexts({ contextTypes: [chrome.runtime.ContextType.OFFSCREEN_DOCUMENT] });
    const response = contexts.length > 0
      ? await chrome.runtime.sendMessage({ kind: "offscreen:get_state" } satisfies InternalMessage).catch(() => undefined)
      : undefined;
    if (!restoredCaptureMatches(state.active_capture.capture_id, response)) {
      restoredCaptureLost = state.active_capture;
      state.active_capture = null;
      await persistState();
    }
  }
}

function updateBadge(): void {
  if (state.active_capture) {
    void chrome.action.setBadgeText({ text: "REC" });
    void chrome.action.setBadgeBackgroundColor({ color: "#c62828" });
  } else if (state.pending_capture) {
    void chrome.action.setBadgeText({ text: "●" });
    void chrome.action.setBadgeBackgroundColor({ color: "#f9a825" });
  } else {
    void chrome.action.setBadgeText({ text: "" });
  }
}

// --- native messaging -------------------------------------------------------

function sendToHost(message: ExtensionToHostMessage): boolean {
  if (!port || state.connection !== "connected") {
    if (message.type !== "hello") return false;
  }
  try {
    port?.postMessage(message);
    return true;
  } catch {
    handleDisconnect();
    return false;
  }
}

function connectNative(): void {
  if (port || state.connection === "connecting") return;
  state.connection = "connecting";
  try {
    port = chrome.runtime.connectNative(NATIVE_HOST_NAME);
  } catch {
    port = null;
    handleDisconnect();
    return;
  }
  port.onMessage.addListener((raw: unknown) => {
    if (isHostMessage(raw)) void handleHostMessage(raw);
  });
  port.onDisconnect.addListener(() => handleDisconnect());
  port.postMessage({
    type: "hello",
    version: PROTOCOL_VERSION,
    extension_version: EXTENSION_VERSION,
    extension_id: chrome.runtime.id,
  } satisfies ExtensionToHostMessage);
}

function handleDisconnect(): void {
  port = null;
  const wasConnected = state.connection === "connected";
  if (state.connection !== "incompatible") state.connection = "disconnected";
  state.host_version = null;
  state.pending_capture = null;
  backpressure.reset();
  void persistState();
  updateBadge();
  if (wasConnected && state.active_capture) {
    // GUI 側が消えたら録音を安全に停止する (取得済み音声は GUI 側が保持する)
    void stopCapture("host_disconnected");
  }
  scheduleReconnect();
}

function scheduleReconnect(): void {
  if (reconnectTimer || state.connection === "incompatible") return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectNative();
  }, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
}

async function handleHostMessage(message: HostToExtensionMessage): Promise<void> {
  switch (message.type) {
    case "hello_ack": {
      if (message.version !== PROTOCOL_VERSION || !message.compatible) {
        state.connection = "incompatible";
        state.host_version = message.host_version;
        port?.disconnect();
        port = null;
        await persistState();
        return;
      }
      state.connection = "connected";
      state.host_version = message.host_version;
      reconnectDelay = RECONNECT_MIN_MS;
      await persistState();
      await sendTabs();
      if (restoredCaptureLost) {
        sendToHost({
          type: "capture_stopped",
          version: PROTOCOL_VERSION,
          capture_id: restoredCaptureLost.capture_id,
          reason: "stream_ended",
          last_seq: restoredCaptureLost.last_seq,
          detail: "service worker再起動時にoffscreen captureを確認できませんでした",
        });
        restoredCaptureLost = null;
      }
      return;
    }
    case "list_tabs":
      await sendTabs();
      return;
    case "request_capture": {
      const tab = await chrome.tabs.get(message.tab_id).catch(() => null);
      if (!tab || !isMeetUrl(tab.url ?? tab.pendingUrl)) {
        sendToHost({
          type: "error",
          version: PROTOCOL_VERSION,
          code: "tab_not_meet",
          message: "指定タブは meet.google.com ではないか、既に閉じられています",
          capture_id: message.capture_id,
        });
        return;
      }
      state.pending_capture = { capture_id: message.capture_id, tab_id: message.tab_id };
      await persistState();
      updateBadge();
      // 対象タブを前面にし、利用者の明示操作 (拡張アイコン → 許可) を待つ
      await chrome.tabs.update(message.tab_id, { active: true }).catch(() => undefined);
      if (tab.windowId !== undefined) {
        await chrome.windows.update(tab.windowId, { focused: true }).catch(() => undefined);
      }
      sendToHost({
        type: "capture_pending",
        version: PROTOCOL_VERSION,
        capture_id: message.capture_id,
        tab_id: message.tab_id,
      });
      return;
    }
    case "stop_capture":
      if (state.active_capture?.capture_id === message.capture_id) {
        await stopCapture("user");
      } else if (state.pending_capture?.capture_id === message.capture_id) {
        state.pending_capture = null;
        await persistState();
        updateBadge();
        sendToHost({
          type: "capture_stopped",
          version: PROTOCOL_VERSION,
          capture_id: message.capture_id,
          reason: "user",
          last_seq: -1,
        });
      }
      return;
    case "ack":
      if (state.active_capture?.capture_id === message.capture_id) {
        backpressure.onAck(message.seq);
      }
      return;
  }
}

async function sendTabs(): Promise<void> {
  const tabs = await chrome.tabs.query({ url: "https://meet.google.com/*" });
  sendToHost({ type: "tabs", version: PROTOCOL_VERSION, tabs: summarizeMeetTabs(tabs) });
}

function scheduleSendTabs(): void {
  if (tabsDebounce) clearTimeout(tabsDebounce);
  tabsDebounce = setTimeout(() => {
    tabsDebounce = null;
    void sendTabs();
  }, 300);
}

// --- offscreen -------------------------------------------------------------

async function ensureOffscreen(): Promise<void> {
  const contexts = await chrome.runtime.getContexts({
    contextTypes: [chrome.runtime.ContextType.OFFSCREEN_DOCUMENT],
  });
  if (contexts.length > 0) return;
  await chrome.offscreen.createDocument({
    url: OFFSCREEN_URL,
    reasons: [chrome.offscreen.Reason.USER_MEDIA],
    justification: "Google Meet タブの音声を取得し、利用者向け再生を維持する",
  });
}

async function startCapture(tabId: number): Promise<{ ok: boolean; error?: string }> {
  if (state.connection !== "connected") {
    return { ok: false, error: "audio-minutes GUI に接続していません" };
  }
  const pending = state.pending_capture;
  if (!pending || pending.tab_id !== tabId) {
    sendToHost({
      type: "error",
      version: PROTOCOL_VERSION,
      code: "no_pending_request",
      message: "GUI からの録音要求がありません。先に GUI で Meet タブを選択してください",
    });
    return { ok: false, error: "GUI で先にタブを選択してください" };
  }
  if (state.active_capture) {
    return { ok: false, error: "既に録音中です" };
  }
  const tab = await chrome.tabs.get(tabId).catch(() => null);
  if (!tab || !isMeetUrl(tab.url ?? tab.pendingUrl)) {
    return { ok: false, error: "このタブは Google Meet ではありません" };
  }
  try {
    const streamId = await new Promise<string>((resolve, reject) => {
      chrome.tabCapture.getMediaStreamId({ targetTabId: tabId }, (id) => {
        if (chrome.runtime.lastError || !id) {
          reject(new Error(chrome.runtime.lastError?.message ?? "stream id を取得できません"));
        } else {
          resolve(id);
        }
      });
    });
    await ensureOffscreen();
    const response = (await chrome.runtime.sendMessage({
      kind: "offscreen:start",
      capture_id: pending.capture_id,
      tab_id: tabId,
      stream_id: streamId,
    } satisfies InternalMessage)) as { ok: boolean; error?: string } | undefined;
    if (!response?.ok) {
      throw new Error(response?.error ?? "offscreen の開始に失敗");
    }
    return { ok: true };
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    sendToHost({
      type: "error",
      version: PROTOCOL_VERSION,
      code: "capture_failed",
      message: "タブ音声の取得に失敗しました",
      capture_id: pending.capture_id,
    });
    state.pending_capture = null;
    await persistState();
    updateBadge();
    return { ok: false, error: detail };
  }
}

async function stopCapture(reason: CaptureStopReason): Promise<void> {
  const active = state.active_capture;
  if (!active) return;
  await chrome.runtime
    .sendMessage({ kind: "offscreen:stop", capture_id: active.capture_id, reason } satisfies InternalMessage)
    .catch(() => undefined);
}

// --- 内部メッセージ (offscreen / popup) ------------------------------------

chrome.runtime.onMessage.addListener((raw: unknown, _sender, sendResponse) => {
  const message = raw as InternalMessage;
  switch (message.kind) {
    case "offscreen:started": {
      const pending = state.pending_capture;
      if (!pending || pending.capture_id !== message.capture_id) return false;
      state.active_capture = {
        capture_id: pending.capture_id,
        tab_id: pending.tab_id,
        started_at_epoch_ms: message.started_at_epoch_ms,
        last_seq: -1,
      };
      state.pending_capture = null;
      backpressure.reset();
      void persistState();
      updateBadge();
      sendToHost({
        type: "capture_started",
        version: PROTOCOL_VERSION,
        capture_id: pending.capture_id,
        tab_id: pending.tab_id,
        sample_rate: AUDIO_SAMPLE_RATE,
        channels: AUDIO_CHANNELS,
        started_at_epoch_ms: message.started_at_epoch_ms,
      });
      return false;
    }
    case "offscreen:chunk": {
      const active = state.active_capture;
      if (!active || active.capture_id !== message.capture_id) return false;
      const sent = sendToHost({
        type: "chunk",
        version: PROTOCOL_VERSION,
        capture_id: message.capture_id,
        seq: message.seq,
        pts_ms: message.pts_ms,
        data: message.data,
        sample_count: message.sample_count,
      });
      if (sent) {
        active.last_seq = message.seq;
        backpressure.onSend(message.seq);
        if (backpressure.exceeded) {
          void stopCapture("backpressure");
        }
      }
      return false;
    }
    case "offscreen:stopped": {
      const active = state.active_capture;
      if (active && active.capture_id === message.capture_id) {
        state.active_capture = null;
        void persistState();
        updateBadge();
        sendToHost({
          type: "capture_stopped",
          version: PROTOCOL_VERSION,
          capture_id: message.capture_id,
          reason: message.reason,
          last_seq: message.last_seq,
          detail: message.detail,
        });
      } else if (state.pending_capture?.capture_id === message.capture_id) {
        // 開始に失敗した
        state.pending_capture = null;
        void persistState();
        updateBadge();
      }
      void chrome.offscreen.closeDocument().catch(() => undefined);
      return false;
    }
    case "popup:get_state":
      sendResponse(state);
      return false;
    case "popup:start_capture":
      startCapture(message.tab_id).then(sendResponse);
      return true;
    case "popup:stop_capture":
      stopCapture("user").then(() => sendResponse({ ok: true }));
      return true;
    default:
      return false;
  }
});

// --- タブイベント ------------------------------------------------------------

chrome.tabs.onRemoved.addListener((tabId) => {
  if (state.active_capture?.tab_id === tabId) {
    void stopCapture("tab_closed");
  }
  if (state.pending_capture?.tab_id === tabId) {
    sendToHost({
      type: "error",
      version: PROTOCOL_VERSION,
      code: "tab_not_meet",
      message: "指定したGoogle Meetタブが録音開始前に閉じられました",
      capture_id: state.pending_capture.capture_id,
    });
    state.pending_capture = null;
    void persistState();
    updateBadge();
  }
  scheduleSendTabs();
});
chrome.tabs.onCreated.addListener(() => scheduleSendTabs());
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (state.pending_capture?.tab_id === tabId && changeInfo.url !== undefined && !isMeetUrl(changeInfo.url)) {
    sendToHost({
      type: "error",
      version: PROTOCOL_VERSION,
      code: "tab_not_meet",
      message: "指定タブが録音開始前にGoogle Meetから移動しました",
      capture_id: state.pending_capture.capture_id,
    });
    state.pending_capture = null;
    void persistState();
    updateBadge();
  }
  // 同じタブ内のページ遷移では録音を継続する (FR-090)。一覧だけ更新する
  if (changeInfo.url !== undefined || changeInfo.title !== undefined || changeInfo.audible !== undefined) {
    scheduleSendTabs();
  }
});

// --- 起動 ---------------------------------------------------------------------

void (async () => {
  await restoreState();
  updateBadge();
  connectNative();
})();

chrome.runtime.onStartup.addListener(() => connectNative());
chrome.runtime.onInstalled.addListener(() => connectNative());
