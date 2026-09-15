/**
 * AudioWorkletProcessor: 入力をモノラルへ mixdown して Float32 のまま main thread へ渡す。
 * リサンプル・量子化・chunk 化は offscreen 側で行う (worklet を軽く保つ)。
 */
import { mixdownToMono } from "./pcm";

declare const sampleRate: number;
declare class AudioWorkletProcessor {
  readonly port: MessagePort;
  constructor();
}
declare function registerProcessor(name: string, ctor: unknown): void;

class MonoTapProcessor extends AudioWorkletProcessor {
  process(inputs: Float32Array[][]): boolean {
    const input = inputs[0];
    if (input && input.length > 0 && input[0].length > 0) {
      const mono = mixdownToMono(input);
      // 転送のためコピーして所有権を渡す
      const copy = mono.slice();
      this.port.postMessage({ samples: copy, sampleRate }, [copy.buffer]);
    }
    return true;
  }
}

registerProcessor("audio-minutes-mono-tap", MonoTapProcessor);
