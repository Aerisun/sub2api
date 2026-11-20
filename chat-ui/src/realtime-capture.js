// The gateway exchanges 24 kHz mono PCM16. The device clock remains native.
const pcmRate = 24000;

class RealtimeCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frame = new Int16Array(960);
    this.active = false;
    this.port.onmessage = ({ data }) => {
      this.active = data === "listen";
      this.phase = 0;
      this.sum = 0;
      this.count = 0;
      this.length = 0;
      this.energy = 0;
    };
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!this.active || !channel) return true;
    for (const sample of channel) {
      this.sum += sample;
      this.count++;
      this.phase += pcmRate;
      if (this.phase < sampleRate) continue;
      const value = Math.max(-1, Math.min(1, this.sum / this.count));
      this.sum = 0;
      this.count = 0;
      while (this.phase >= sampleRate) {
        this.phase -= sampleRate;
        this.frame[this.length++] = value < 0 ? value * 32768 : value * 32767;
        this.energy += value * value;
        if (this.length === this.frame.length) {
          const pcm = this.frame.buffer;
          this.port.postMessage({ pcm, rms: Math.sqrt(this.energy / this.length) }, [pcm]);
          this.frame = new Int16Array(960);
          this.length = 0;
          this.energy = 0;
        }
      }
    }
    return true;
  }
}

// Splice at matching waveform phases, then crossfade over 4 ms. This
// changes speech duration without shifting the pitch of the entire stream.
function overlapSplice(chunk, requested) {
  const overlap = 96;
  const size = Math.abs(requested);
  if (size < 48 || chunk.length < 240) return chunk;
  const maxLag = Math.min(chunk.length - overlap - 32, Math.max(320, Math.ceil(size * 1.6)));
  const minLag = Math.min(maxLag, Math.max(32, Math.floor(size * 0.5)));
  const start = Math.floor((chunk.length - overlap - maxLag) / 2);
  let bestLag = minLag, bestScore = Infinity;
  for (let lag = minLag; lag <= maxLag; lag++) {
    let error = 0, energy = 1;
    for (let i = start; i < start + overlap; i += 4) {
      const a = chunk[i], b = chunk[i + lag];
      error += (a - b) * (a - b);
      energy += a * a + b * b;
    }
    const score = error / energy + 0.025 * Math.abs(lag - size) / Math.max(96, size);
    if (score < bestScore) { bestScore = score; bestLag = lag; }
  }
  const extra = requested > 0 ? bestLag : -bestLag;
  const a = extra > 0 ? start + bestLag : start;
  const b = a - extra;
  const result = new Int16Array(chunk.length + extra);
  result.set(chunk.subarray(0, a));
  for (let i = 0; i < overlap; i++) {
    const mix = i / (overlap - 1);
    result[a + i] = chunk[a + i] * (1 - mix) + chunk[b + i] * mix;
  }
  result.set(chunk.subarray(b + overlap), a + overlap);
  return result;
}

class RealtimePlayback extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunks = [];
    this.queuedSamples = 0;
    this.position = 0;
    this.step = pcmRate / sampleRate;
    this.targetSeconds = 0.36;
    this.adjustDebt = 0;
    this.expanded = 0;
    this.compressed = 0;
    this.firstArrival = null;
    this.received = 0;
    this.maxLag = 0;
    this.playing = false;
    this.started = false;
    this.end = false;
    this.emptyFrames = 0;
    this.gapFrames = 0;
    this.underruns = 0;
    this.port.onmessage = ({ data }) => {
      if (data === "end") {
        this.end = true;
      } else if (data.byteLength) {
        const chunk = new Int16Array(data);
        if (this.firstArrival === null) this.firstArrival = currentFrame;
        const elapsed = (currentFrame - this.firstArrival) / sampleRate;
        this.maxLag = Math.max(this.maxLag, elapsed - this.received / pcmRate);
        this.received += chunk.length;
        const ahead = (this.queuedSamples - this.position) / pcmRate;
        // A small deadband leaves healthy speech untouched. Catch up gently
        // when network packets arrive together after a delay.
        let ratio = 1;
        if (this.started && ahead < 0.36) ratio += Math.min(0.40, (0.36 - ahead) * 2);
        if (this.started && ahead > 0.48) ratio -= Math.min(0.14, (ahead - 0.48) * 0.7);
        for (let offset = 0; offset < chunk.length; offset += 480) {
          const slice = chunk.subarray(offset, offset + 480);
          this.adjustDebt += (ratio - 1) * slice.length;
          const adjusted = this.adjustDebt * (ratio - 1) > 0 ? overlapSplice(slice, this.adjustDebt) : slice;
          const extra = adjusted.length - slice.length;
          this.adjustDebt -= extra;
          this.expanded += Math.max(0, extra);
          this.compressed += Math.max(0, -extra);
          this.chunks.push(adjusted);
          this.queuedSamples += adjusted.length;
        }
      }
    };
  }

  process(_, outputs) {
    const output = outputs[0][0];
    if (!this.playing && this.queuedSamples) {
      if (this.end || this.queuedSamples - this.position >= (this.started ? 0.12 : this.targetSeconds) * pcmRate) {
        this.playing = true;
        if (this.started) {
          this.underruns++;
          this.gapFrames += this.emptyFrames;
        } else {
          this.started = true;
          this.port.postMessage({ type: "playing" });
        }
        this.emptyFrames = 0;
      }
    }

    let written = 0;
    while (this.playing && this.chunks.length && written < output.length) {
      const chunk = this.chunks[0];
      const last = chunk.length - 1;
      const next = this.chunks[1]?.[0] ?? chunk[last];
      while (written < output.length && this.position < chunk.length) {
        const index = Math.floor(this.position);
        const a = chunk[index];
        const b = index === last ? next : chunk[index + 1];
        output[written++] = (a + (b - a) * (this.position - index)) / 32768;
        this.position += this.step;
      }
      if (this.position >= chunk.length) {
        this.position -= chunk.length;
        this.queuedSamples -= chunk.length;
        this.chunks.shift();
      }
    }

    if (this.end && !this.queuedSamples) {
      this.port.postMessage({ type: "drained", underruns: this.underruns, gapFrames: this.gapFrames,
        sampleRate, sourceSamples: this.received, expandedSamples: this.expanded, compressedSamples: this.compressed });
      this.position = 0;
      this.playing = this.started = this.end = false;
      this.emptyFrames = this.gapFrames = this.underruns = 0;
      // Use the arrival clock relative to the PCM clock. Several small stalls
      // can accumulate even when individual packets remain close together.
      this.targetSeconds = Math.min(0.40, Math.max(0.20, this.targetSeconds - 0.06, this.maxLag + 0.04));
      this.adjustDebt = this.expanded = this.compressed = 0;
      this.firstArrival = null;
      this.received = this.maxLag = 0;
    } else if (this.started && written < output.length) {
      // Only count an underrun if more PCM
      // arrives in this turn; a delayed response.done is not a gap.
      this.playing = false;
      this.emptyFrames += output.length - written;
    }
    return true;
  }
}

registerProcessor("realtime-capture", RealtimeCapture);
registerProcessor("realtime-playback", RealtimePlayback);
