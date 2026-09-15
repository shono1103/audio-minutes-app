/** PCM 処理の純粋関数。AudioWorklet と offscreen document、テストで共有する。 */

/** 複数チャンネルを平均してモノラルにする。 */
export function mixdownToMono(channels: Float32Array[]): Float32Array {
  if (channels.length === 0) return new Float32Array(0);
  if (channels.length === 1) return channels[0];
  const length = channels[0].length;
  const out = new Float32Array(length);
  for (const channel of channels) {
    for (let i = 0; i < length; i++) out[i] += channel[i];
  }
  const scale = 1 / channels.length;
  for (let i = 0; i < length; i++) out[i] *= scale;
  return out;
}

/** 線形補間による連続リサンプラー。ブロック境界の位相を保持する。 */
export class LinearResampler {
  private readonly ratio: number;
  private position = 0; // 入力サンプル座標での次の出力位置
  private last: number | null = null; // 前ブロック末尾のサンプル (補間用)

  constructor(readonly inputRate: number, readonly outputRate: number) {
    if (inputRate <= 0 || outputRate <= 0) throw new Error("sample rate must be positive");
    this.ratio = inputRate / outputRate;
  }

  process(input: Float32Array): Float32Array {
    if (input.length === 0) return new Float32Array(0);
    if (this.inputRate === this.outputRate) return input.slice();
    // 前ブロックの末尾を先頭に付けて連続性を保つ
    const hasLast = this.last !== null;
    const buffer = hasLast ? new Float32Array(input.length + 1) : input;
    if (hasLast) {
      buffer[0] = this.last as number;
      buffer.set(input, 1);
    }
    const out: number[] = [];
    // this.position は「前ブロック末尾サンプルを index 0 とした」バッファ座標。carry が無い初回だけ input 座標と一致する
    let pos = this.position;
    while (Math.floor(pos) + 1 < buffer.length) {
      const index = Math.floor(pos);
      const frac = pos - index;
      out.push(buffer[index] * (1 - frac) + buffer[index + 1] * frac);
      pos += this.ratio;
    }
    // 次ブロックでは buffer の末尾サンプルが index 0 になるので、その分だけ位置を戻す
    this.position = pos - (buffer.length - 1);
    this.last = buffer[buffer.length - 1];
    return Float32Array.from(out);
  }
}

export function floatToInt16(input: Float32Array): Int16Array {
  const out = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    const clamped = Math.max(-1, Math.min(1, input[i]));
    out[i] = clamped < 0 ? Math.round(clamped * 0x8000) : Math.round(clamped * 0x7fff);
  }
  return out;
}

/** 固定サンプル数の chunk に切り出す。端数は次回へ繰り越す。 */
export class Chunker {
  private pending: Int16Array = new Int16Array(0);

  constructor(readonly samplesPerChunk: number) {
    if (samplesPerChunk <= 0) throw new Error("samplesPerChunk must be positive");
  }

  push(samples: Int16Array): Int16Array[] {
    const merged = new Int16Array(this.pending.length + samples.length);
    merged.set(this.pending, 0);
    merged.set(samples, this.pending.length);
    const chunks: Int16Array[] = [];
    let offset = 0;
    while (merged.length - offset >= this.samplesPerChunk) {
      chunks.push(merged.slice(offset, offset + this.samplesPerChunk));
      offset += this.samplesPerChunk;
    }
    this.pending = merged.slice(offset);
    return chunks;
  }

  /** 停止時に残りを 0 詰めせずそのまま返す (空なら null)。 */
  flush(): Int16Array | null {
    if (this.pending.length === 0) return null;
    const rest = this.pending;
    this.pending = new Int16Array(0);
    return rest;
  }
}

/** 連番と pts_ms を採番する。pts は送出済みサンプル数から算出し、壁時計に依存しない。 */
export class SequenceTracker {
  private seq = 0;
  private samplesEmitted = 0;

  constructor(readonly sampleRate: number) {}

  next(sampleCount: number): { seq: number; pts_ms: number } {
    const result = { seq: this.seq, pts_ms: Math.floor((this.samplesEmitted * 1000) / this.sampleRate) };
    this.seq += 1;
    this.samplesEmitted += sampleCount;
    return result;
  }

  get lastSeq(): number {
    return this.seq - 1;
  }
}

/** host からの ack と送信済み seq を照合し、未 ack が上限を超えたら停止判定にする。 */
export class Backpressure {
  private readonly pending = new Set<number>();

  constructor(readonly maxPending: number) {}

  onSend(seq: number): void {
    this.pending.add(seq);
  }

  /** seq 以下をすべて確認済みとみなす (累積 ack)。 */
  onAck(seq: number): void {
    for (const value of Array.from(this.pending)) {
      if (value <= seq) this.pending.delete(value);
    }
  }

  get pendingCount(): number {
    return this.pending.size;
  }

  get exceeded(): boolean {
    return this.pending.size > this.maxPending;
  }

  reset(): void {
    this.pending.clear();
  }
}

export function int16ToBase64(samples: Int16Array): string {
  const bytes = new Uint8Array(samples.buffer, samples.byteOffset, samples.byteLength);
  let binary = "";
  const step = 0x8000;
  for (let i = 0; i < bytes.length; i += step) {
    binary += String.fromCharCode(...bytes.subarray(i, i + step));
  }
  return btoa(binary);
}

export function base64ToInt16(value: string): Int16Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Int16Array(bytes.buffer, 0, bytes.length / 2);
}
