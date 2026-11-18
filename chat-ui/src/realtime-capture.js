/* AudioWorklet renders in the browser's audio thread. Output PCM is 24 kHz mono. */
class RealtimeCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.phase = 0;
    this.sum = 0;
    this.count = 0;
    this.frame = new Int16Array(1920);
    this.length = 0;
    this.energy = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) return true;
    for (const sample of channel) {
      this.sum += sample;
      this.count++;
      this.phase += 24000;
      if (this.phase < sampleRate) continue;
      this.phase -= sampleRate;
      const value = Math.max(-1, Math.min(1, this.sum / this.count));
      this.frame[this.length++] = value < 0 ? value * 32768 : value * 32767;
      this.energy += value * value;
      this.sum = 0;
      this.count = 0;
      if (this.length === this.frame.length) {
        const pcm = this.frame.buffer;
        this.port.postMessage({pcm, rms: Math.sqrt(this.energy / this.length)}, [pcm]);
        this.frame = new Int16Array(1920);
        this.length = 0;
        this.energy = 0;
      }
    }
    return true;
  }
}

registerProcessor("realtime-capture", RealtimeCapture);
