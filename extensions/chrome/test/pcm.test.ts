import { describe, expect, it } from "vitest";

import { MAX_PENDING_CHUNKS, SAMPLES_PER_CHUNK } from "../src/messages";
import {
  Backpressure,
  Chunker,
  LinearResampler,
  SequenceTracker,
  base64ToInt16,
  floatToInt16,
  int16ToBase64,
  mixdownToMono,
} from "../src/pcm";

describe("mixdownToMono", () => {
  it("2ch を平均する", () => {
    const mono = mixdownToMono([Float32Array.from([1, 0.5]), Float32Array.from([0, 0.5])]);
    expect(Array.from(mono)).toEqual([0.5, 0.5]);
  });
  it("1ch はそのまま", () => {
    const input = Float32Array.from([0.1, 0.2]);
    expect(mixdownToMono([input])).toBe(input);
  });
});

describe("LinearResampler", () => {
  it("48 kHz → 16 kHz でサンプル数が約 1/3 になり、ブロック境界で連続する", () => {
    const resampler = new LinearResampler(48000, 16000);
    const total = 48000; // 1 秒
    let outCount = 0;
    const block = 128;
    const outputs: number[] = [];
    for (let offset = 0; offset < total; offset += block) {
      const input = new Float32Array(block);
      for (let i = 0; i < block; i++) input[i] = Math.sin((2 * Math.PI * 440 * (offset + i)) / 48000);
      const out = resampler.process(input);
      outCount += out.length;
      outputs.push(...out);
    }
    expect(Math.abs(outCount - 16000)).toBeLessThanOrEqual(2);
    // 440 Hz の正弦波を保持しているか: 隣接差の最大値が不連続 (>0.5) を持たない
    let maxJump = 0;
    for (let i = 1; i < outputs.length; i++) maxJump = Math.max(maxJump, Math.abs(outputs[i] - outputs[i - 1]));
    expect(maxJump).toBeLessThan(0.3);
  });
  it("同じ rate はコピーを返す", () => {
    const resampler = new LinearResampler(16000, 16000);
    const input = Float32Array.from([0.1, 0.2, 0.3]);
    const out = resampler.process(input);
    expect(Array.from(out)).toEqual([0.1, 0.2, 0.3].map((v) => Math.fround(v)));
    expect(out).not.toBe(input);
  });
});

describe("floatToInt16", () => {
  it("クリップして量子化する", () => {
    expect(Array.from(floatToInt16(Float32Array.from([1, -1, 0, 2, -2])))).toEqual([32767, -32768, 0, 32767, -32768]);
  });
});

describe("Chunker", () => {
  it("200 ms (3200 サンプル) ごとに切り出し、端数を繰り越す", () => {
    const chunker = new Chunker(SAMPLES_PER_CHUNK);
    expect(chunker.push(new Int16Array(3000))).toHaveLength(0);
    const chunks = chunker.push(new Int16Array(3500));
    expect(chunks).toHaveLength(2);
    expect(chunks[0]).toHaveLength(3200);
    const rest = chunker.flush();
    expect(rest?.length).toBe(100);
    expect(chunker.flush()).toBeNull();
  });
});

describe("SequenceTracker", () => {
  it("連番と pts_ms をサンプル数から採番する", () => {
    const tracker = new SequenceTracker(16000);
    expect(tracker.next(3200)).toEqual({ seq: 0, pts_ms: 0 });
    expect(tracker.next(3200)).toEqual({ seq: 1, pts_ms: 200 });
    expect(tracker.next(100)).toEqual({ seq: 2, pts_ms: 400 });
    expect(tracker.lastSeq).toBe(2);
  });
});

describe("Backpressure", () => {
  it("未 ack が上限を超えたら exceeded、累積 ack で解消する", () => {
    const bp = new Backpressure(MAX_PENDING_CHUNKS);
    for (let seq = 0; seq <= MAX_PENDING_CHUNKS; seq++) bp.onSend(seq);
    expect(bp.pendingCount).toBe(MAX_PENDING_CHUNKS + 1);
    expect(bp.exceeded).toBe(true);
    bp.onAck(10);
    expect(bp.pendingCount).toBe(MAX_PENDING_CHUNKS - 10);
    expect(bp.exceeded).toBe(false);
    bp.onAck(MAX_PENDING_CHUNKS);
    expect(bp.pendingCount).toBe(0);
  });
  it("上限ちょうどでは停止しない", () => {
    const bp = new Backpressure(3);
    bp.onSend(0);
    bp.onSend(1);
    bp.onSend(2);
    expect(bp.exceeded).toBe(false);
    bp.onSend(3);
    expect(bp.exceeded).toBe(true);
  });
});

describe("base64", () => {
  it("往復で一致する", () => {
    const samples = Int16Array.from([0, 1, -1, 32767, -32768, 1234]);
    expect(Array.from(base64ToInt16(int16ToBase64(samples)))).toEqual(Array.from(samples));
  });
});
