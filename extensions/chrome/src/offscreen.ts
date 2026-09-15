/**
 * offscreen document: tabCapture の stream を受け取り、利用者向け再生を維持したまま
 * PCM16 16 kHz mono の 200 ms chunk を background へ送る。
 */
import {
  AUDIO_SAMPLE_RATE,
  SAMPLES_PER_CHUNK,
  type CaptureStopReason,
  type InternalMessage,
  type OffscreenChunk,
  type OffscreenStarted,
  type OffscreenStopped,
} from "./messages";
import { Chunker, LinearResampler, SequenceTracker, floatToInt16, int16ToBase64 } from "./pcm";

interface ActiveCapture {
  captureId: string;
  stream: MediaStream;
  context: AudioContext;
  node: AudioWorkletNode;
  source: MediaStreamAudioSourceNode;
  resampler: LinearResampler;
  chunker: Chunker;
  tracker: SequenceTracker;
  stopped: boolean;
}

let active: ActiveCapture | null = null;

function send(message: InternalMessage): Promise<unknown> {
  return chrome.runtime.sendMessage(message).catch(() => {
    // background再起動中の欠落はGUI側のseq検査で検出し、安全に停止する。
  });
}

async function start(captureId: string, streamId: string): Promise<void> {
  if (active) {
    await stop("error", "既に別の capture が実行中");
  }
  let stream: MediaStream | null = null;
  let context: AudioContext | null = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        // Chrome 固有の制約。tabCapture.getMediaStreamId の結果を渡す
        mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId },
      } as MediaTrackConstraints,
      video: false,
    });
    context = new AudioContext();
    await context.audioWorklet.addModule(chrome.runtime.getURL("worklet.js"));
  } catch (error) {
    stream?.getTracks().forEach((track) => track.stop());
    if (context && context.state !== "closed") await context.close().catch(() => undefined);
    throw error;
  }
  const source = context.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(context, "audio-minutes-mono-tap", {
    numberOfInputs: 1,
    numberOfOutputs: 0,
  });
  source.connect(node);
  // 利用者向け再生を維持する (tabCapture 中はタブ側の出力が止まるため offscreen で再生する)
  source.connect(context.destination);

  const capture: ActiveCapture = {
    captureId,
    stream,
    context,
    node,
    source,
    resampler: new LinearResampler(context.sampleRate, AUDIO_SAMPLE_RATE),
    chunker: new Chunker(SAMPLES_PER_CHUNK),
    tracker: new SequenceTracker(AUDIO_SAMPLE_RATE),
    stopped: false,
  };
  active = capture;

  node.port.onmessage = (event: MessageEvent<{ samples: Float32Array }>) => {
    if (capture.stopped) return;
    const resampled = capture.resampler.process(event.data.samples);
    const pcm = floatToInt16(resampled);
    for (const chunk of capture.chunker.push(pcm)) {
      const { seq, pts_ms } = capture.tracker.next(chunk.length);
      const message: OffscreenChunk = {
        kind: "offscreen:chunk",
        capture_id: captureId,
        seq,
        pts_ms,
        data: int16ToBase64(chunk),
        sample_count: chunk.length,
      };
      void send(message);
    }
  };

  for (const track of stream.getAudioTracks()) {
    track.addEventListener("ended", () => {
      void stop("stream_ended", "タブの音声ストリームが終了");
    });
  }

  const started: OffscreenStarted = {
    kind: "offscreen:started",
    capture_id: captureId,
    started_at_epoch_ms: Date.now(),
  };
  await send(started);
}

async function stop(reason: CaptureStopReason, detail?: string): Promise<void> {
  const capture = active;
  if (!capture || capture.stopped) return;
  capture.stopped = true;
  active = null;
  try {
    capture.node.port.onmessage = null;
    capture.source.disconnect();
    capture.node.disconnect();
    for (const track of capture.stream.getTracks()) track.stop();
    await capture.context.close();
  } catch {
    // 停止処理の失敗は結果に影響しない
  }
  // 端数は 200 ms に満たないためそのまま最後の chunk として送る
  const rest = capture.chunker.flush();
  if (rest && rest.length > 0) {
    const { seq, pts_ms } = capture.tracker.next(rest.length);
    // 最終chunkの配送完了後にstoppedを送る。GUIはこの順序でWAVを確定する。
    await send({
      kind: "offscreen:chunk",
      capture_id: capture.captureId,
      seq,
      pts_ms,
      data: int16ToBase64(rest),
      sample_count: rest.length,
    });
  }
  const stopped: OffscreenStopped = {
    kind: "offscreen:stopped",
    capture_id: capture.captureId,
    reason,
    last_seq: capture.tracker.lastSeq,
    detail,
  };
  await send(stopped);
}

chrome.runtime.onMessage.addListener((raw: unknown, _sender, sendResponse) => {
  const message = raw as InternalMessage;
  if (message.kind === "offscreen:get_state") {
    sendResponse({ active_capture_id: active?.captureId ?? null });
    return false;
  }
  if (message.kind === "offscreen:start") {
    start(message.capture_id, message.stream_id)
      .then(() => sendResponse({ ok: true }))
      .catch((error: unknown) => {
        const detail = error instanceof Error ? error.message : String(error);
        active = null;
        void send({
          kind: "offscreen:stopped",
          capture_id: message.capture_id,
          reason: "error",
          last_seq: -1,
          detail,
        });
        sendResponse({ ok: false, error: detail });
      });
    return true;
  }
  if (message.kind === "offscreen:stop") {
    stop(message.reason)
      .then(() => sendResponse({ ok: true }))
      .catch(() => sendResponse({ ok: false }));
    return true;
  }
  return false;
});
