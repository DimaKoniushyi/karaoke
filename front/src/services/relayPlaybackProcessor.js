// Companion to pythonVoiceRelay.js: a small ring-buffer jitter buffer that
// plays back Float32 sample chunks delivered over the microphone relay
// WebSocket (see audio_relay_protocol.py on the backend) as they arrive via
// port.onmessage, instead of the usual microphone-driven audio graph input.
//
// Incoming chunks are at the Python side's sample rate (processorOptions.
// sourceRate), which does not always match this worklet's actual sampleRate
// -- the browser does not always honor the rate pythonVoiceRelay.js requests
// the AudioContext at. Samples are resampled on ingest (linear
// interpolation, the same technique and phase-accumulator design as the
// native WASAPI engine's MonitorBuffer::pop in monitor_buffer.h) so playback
// pitch/speed stays correct either way, and process() just reads out
// already-correct-rate samples.
//
// A live duet is two people singing together, not a podcast -- letting the
// backlog climb toward a full second means the room partner hears the
// singer's voice up to ~1s late before this ever starts dropping anything.
// Losing a little audio to a brief stall is far less disruptive to live
// singing than that much added lag, so the queued-latency cap is
// deliberately tight.
const MAX_QUEUED_SECONDS = 0.02;
const REPORT_INTERVAL_SAMPLES = Math.round(sampleRate * 0.5);

class RelayPlaybackProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const sourceRate = options?.processorOptions?.sourceRate || sampleRate;
    // Source samples consumed per output sample -- <1 means upsampling
    // (source slower than output), >1 means downsampling.
    this.ratio = sourceRate / sampleRate;
    // Ring buffer capacity is generous (0.5s at the source rate) so a single
    // large delivery never gets clipped by capacity itself; the tighter
    // MAX_QUEUED_SECONDS cap below is what actually bounds live latency.
    this.capacity = Math.max(64, Math.round(sourceRate * 0.5));
    this.ring = new Float32Array(this.capacity);
    this.head = 0;
    this.used = 0;
    this.phase = 0;
    this.maxQueuedSamples = Math.max(2, Math.round(sourceRate * MAX_QUEUED_SECONDS));
    this.underruns = 0;
    this.dropped = 0;
    this.samplesSinceReport = 0;

    this.port.onmessage = ({ data }) => {
      if (!(data instanceof Float32Array) || !data.length) return;
      this.push(data);
    };
  }

  push(chunk) {
    for (let i = 0; i < chunk.length; i += 1) {
      if (this.used === this.capacity) {
        this.head = (this.head + 1) % this.capacity;
        this.used -= 1;
        this.dropped += 1;
        this.phase = 0;
      }
      this.ring[(this.head + this.used) % this.capacity] = chunk[i];
      this.used += 1;
    }
    // Enforce the tight live-latency cap independently of raw ring capacity:
    // prefer losing the oldest queued audio over ever playing this delayed
    // by more than MAX_QUEUED_SECONDS.
    while (this.used > this.maxQueuedSamples) {
      this.head = (this.head + 1) % this.capacity;
      this.used -= 1;
      this.dropped += 1;
      this.phase = 0;
    }
  }

  pop() {
    if (this.ratio === 1) {
      if (!this.used) return null;
      const value = this.ring[this.head];
      this.head = (this.head + 1) % this.capacity;
      this.used -= 1;
      return value;
    }
    const consume = Math.floor(this.phase + this.ratio);
    if (this.used < Math.max(2, consume)) return null;
    const a = this.ring[this.head];
    const b = this.ring[(this.head + 1) % this.capacity];
    const value = a * (1 - this.phase) + b * this.phase;
    this.phase += this.ratio - consume;
    this.head = (this.head + consume) % this.capacity;
    this.used -= consume;
    return value;
  }

  report() {
    // Previously nothing measured relay underruns/backlog at all -- remote
    // voice could visibly crackle or drop out with zero diagnostic signal.
    // Throttled and best-effort: dropped delivery of a status message must
    // never affect playback.
    try {
      this.port.postMessage({
        underruns: this.underruns,
        dropped: this.dropped,
        queuedMs: (this.used / this.ratio / sampleRate) * 1000
      });
    } catch {
      // Ignore -- diagnostics are not allowed to affect playback.
    }
  }

  process(_inputs, outputs) {
    const channel = outputs[0]?.[0];
    if (!channel) return true;
    for (let index = 0; index < channel.length; index += 1) {
      const value = this.pop();
      if (value == null) {
        channel[index] = 0;
        this.underruns += 1;
      } else {
        channel[index] = value;
      }
    }
    this.samplesSinceReport += channel.length;
    if (this.samplesSinceReport >= REPORT_INTERVAL_SAMPLES) {
      this.samplesSinceReport = 0;
      this.report();
    }
    return true;
  }
}

registerProcessor("advoice-relay-playback", RelayPlaybackProcessor);
