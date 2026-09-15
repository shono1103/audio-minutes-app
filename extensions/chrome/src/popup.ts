/** popup: 連携状態と録音状態を表示し、利用者の明示操作で capture を開始・停止する。 */
import type { ExtensionState, InternalMessage } from "./messages";
import { isMeetUrl } from "./tabs";

const connectionEl = document.getElementById("connection") as HTMLElement;
const tabEl = document.getElementById("tab") as HTMLElement;
const captureEl = document.getElementById("capture") as HTMLElement;
const startButton = document.getElementById("start") as HTMLButtonElement;
const stopButton = document.getElementById("stop") as HTMLButtonElement;
const noteEl = document.getElementById("note") as HTMLElement;

function setStatus(element: HTMLElement, text: string, level: "ok" | "warn" | "err" | ""): void {
  element.textContent = text;
  element.className = `status ${level}`.trim();
}

async function currentTab(): Promise<chrome.tabs.Tab | null> {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab ?? null;
}

async function refresh(): Promise<void> {
  const state = (await chrome.runtime.sendMessage({ kind: "popup:get_state" } satisfies InternalMessage)) as
    | ExtensionState
    | undefined;
  const tab = await currentTab();
  const tabIsMeet = !!tab && isMeetUrl(tab.url ?? tab.pendingUrl);

  if (!state) {
    setStatus(connectionEl, "拡張の初期化中", "warn");
    startButton.disabled = true;
    return;
  }
  switch (state.connection) {
    case "connected":
      setStatus(connectionEl, `接続済み (host ${state.host_version ?? "?"})`, "ok");
      break;
    case "incompatible":
      setStatus(connectionEl, `版不一致 (host ${state.host_version ?? "?"})。GUI と拡張を更新してください`, "err");
      break;
    case "connecting":
      setStatus(connectionEl, "接続中", "warn");
      break;
    default:
      setStatus(connectionEl, "未接続。audio-minutes GUI を起動してください", "err");
  }
  setStatus(tabEl, tabIsMeet ? "Google Meet" : "Google Meet ではありません", tabIsMeet ? "ok" : "warn");

  if (state.active_capture) {
    const own = tab?.id === state.active_capture.tab_id;
    setStatus(captureEl, own ? "このタブを録音中" : "別のタブを録音中", "ok");
    startButton.hidden = true;
    stopButton.hidden = false;
  } else {
    startButton.hidden = false;
    stopButton.hidden = true;
    const pendingForThisTab = !!state.pending_capture && state.pending_capture.tab_id === tab?.id;
    if (pendingForThisTab) {
      setStatus(captureEl, "GUI が許可を待っています", "warn");
      startButton.disabled = state.connection !== "connected" || !tabIsMeet;
      noteEl.textContent = "「Chrome で録音を許可」を押すと、このタブの音声だけを GUI へ渡し始めます。再生は維持されます。";
    } else {
      setStatus(captureEl, "停止中", "");
      startButton.disabled = true;
      noteEl.textContent = state.pending_capture
        ? "GUI が選択した Meet タブに切り替えてから許可してください。"
        : "先に audio-minutes GUI で録音対象の Meet タブを選択してください。";
    }
  }
}

startButton.addEventListener("click", async () => {
  const tab = await currentTab();
  if (!tab?.id) return;
  startButton.disabled = true;
  const result = (await chrome.runtime.sendMessage({
    kind: "popup:start_capture",
    tab_id: tab.id,
  } satisfies InternalMessage)) as { ok: boolean; error?: string } | undefined;
  if (!result?.ok) {
    setStatus(captureEl, result?.error ?? "開始できませんでした", "err");
  }
  await refresh();
});

stopButton.addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ kind: "popup:stop_capture" } satisfies InternalMessage);
  await refresh();
});

void refresh();
setInterval(() => void refresh(), 1500);
