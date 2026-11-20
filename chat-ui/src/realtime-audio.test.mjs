import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";

const source = readFileSync(new URL("./realtime-capture.js", import.meta.url), "utf8");

function newProcessor(name, sampleRate) {
  const processors = {};
  const events = [];
  class AudioWorkletProcessor {
    port = { postMessage: event => events.push(event) };
  }
  const clock = {
    AudioWorkletProcessor, sampleRate, currentFrame: 0,
    registerProcessor: (name, processor) => { processors[name] = processor; },
  };
  runInNewContext(source, clock);
  return { processor: new processors[name](), events, clock };
}

function replay(arrivals, rate = 48000, sample) {
  const { processor, events, clock } = newProcessor("realtime-playback", rate);
  const played = [];
  let packet = 0;
  let sentEnd = false;
  // 480 samples / 24 kHz = 20 ms of audio in each packet.
  for (let frame = 0; frame < rate * (arrivals.at(-1) / 1000 + 10); frame += 128) {
    clock.currentFrame = frame;
    const ms = frame * 1000 / rate;
    while (packet < arrivals.length && arrivals[packet] <= ms) {
      const pcm = new Int16Array(480).fill(1000 + packet);
      if (sample) {
        for (let i = 0; i < pcm.length; i++) pcm[i] = sample(packet * pcm.length + i);
      }
      processor.port.onmessage({ data: pcm.buffer });
      packet++;
    }
    if (!sentEnd && ms >= arrivals.at(-1) + 400) {
      sentEnd = true;
      processor.port.onmessage({ data: "end" });
    }
    const output = new Float32Array(128);
    processor.process([], [[output]]);
    played.push(...output);
    if (events.at(-1)?.type === "drained") break;
  }
  const first = played.findIndex(v => v !== 0);
  const last = played.findLastIndex(v => v !== 0);
  return { played: played.slice(first, last + 1), events, first };
}

test("20 ms speech packets stay continuous through 120 ms of arrival jitter", () => {
  const arrivals = Array.from({ length: 50 }, (_, i) => i * 20);
  for (let i = 20; i < 26; i++) arrivals[i] = 520;
  for (const rate of [24000, 44100, 48000]) {
    const { played, events, first } = replay(arrivals, rate);
    assert(first >= 0 && first / rate < 0.43);
    assert.equal(events.at(-1).sourceSamples, 24000);
    assert(played.every(sample => sample > 0), "No inserted silence during the response");
    assert.equal(events[0].type, "playing");
    const drained = events.at(-1);
    assert.equal(drained.type, "drained");
    assert.equal(drained.underruns, 0);
    assert.equal(drained.gapFrames, 0);
  }
});

test("several consecutive network delays totaling 340 ms do not split a sentence", () => {
  const arrivals = Array.from({ length: 276 }, (_, packet) => {
    let delay = 0;
    if (packet >= 125) delay += 120;
    if (packet >= 180) delay += 160;
    if (packet >= 190) delay += 60;
    return packet * 20 + delay;
  });
  const { played, events } = replay(arrivals);
  assert.equal(events.at(-1).sourceSamples, 276 * 480);
  assert(played.every(sample => sample > 0));
  assert.equal(events.at(-1).underruns, 0);
});

test("a stalled stream re-buffers once and drains a short final tail", () => {
  const arrivals = Array.from({ length: 35 }, (_, i) => i * 20 + (i >= 25 ? 700 : 0));
  const { played, events } = replay(arrivals);
  const gaps = played.filter(value => value === 0).length;
  const stats = events.at(-1);
  assert.equal(stats.sourceSamples, arrivals.length * 480);
  assert.equal(played.length - gaps, (stats.sourceSamples + stats.expandedSamples - stats.compressedSamples) * 2);
  assert(gaps > 0);
  assert.equal(events.at(-1).type, "drained");
  assert.equal(events.at(-1).underruns, 1);
  assert.equal(events.at(-1).gapFrames, gaps);
  const short = replay([0]);
  assert.equal(short.played.length, 960);
  assert.equal(short.events.at(-1).underruns, 0);
});

test("long speech retains its pitch through sustained delay and catches up after a burst", () => {
  const arrivals = Array.from({ length: 3000 }, (_, i) => {
    if (i < 1000) return i * 24;
    if (i < 1700) return 24000 + (i - 1000) * 14;
    return 33800 + (i - 1700) * 20;
  });
  const rate = 24000, hz = 165;
  const { played, events, first } = replay(arrivals, rate, i =>
    9000 * Math.sin(2 * Math.PI * (i + 0.3) * hz / rate));
  const stats = events.at(-1);
  assert.equal(stats.sourceSamples, rate * 60);
  assert.equal(stats.gapFrames, 0);
  assert(stats.expandedSamples > rate && stats.compressedSamples > rate);
  assert(first / rate < 0.45);
  assert((first + played.length) / rate - arrivals.at(-1) / 1000 < 1.6);
  let rising = 0;
  for (let i = 1; i < played.length; i++) {
    if (played[i] > 0 && played[i - 1] <= 0) rising++;
  }
  assert(Math.abs(rising * rate / played.length - hz) < 1.5);
});

test("muted capture discards partial frames before the next recording", () => {
  const { processor, events } = newProcessor("realtime-capture", 48000);
  const process = (samples, amplitude) => processor.process([[new Float32Array(samples).fill(amplitude)]]);
  process(4800, 1);
  processor.port.onmessage({ data: "listen" });
  process(1600, 1);
  processor.port.onmessage({ data: "pause" });
  process(3200, 1);
  assert.equal(events.length, 0);
  processor.port.onmessage({ data: "listen" });
  process(1920, 0.25);
  assert.equal(events.length, 1);
  assert.equal(events[0].rms, 0.25);
  assert.equal(new Int16Array(events[0].pcm).length, 960);
});
