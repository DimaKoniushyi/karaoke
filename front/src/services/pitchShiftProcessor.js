class AdVoicePitchShiftProcessor extends AudioWorkletProcessor {
  static get parameterDescriptors() {
    return [
      { name: "ratio", defaultValue: 1, minValue: 0.5, maxValue: 2, automationRate: "k-rate" }
    ];
  }

  constructor() {
    super();
    // Keep the browser room fallback aligned with the native/Python monitor:
    // the two overlap-add heads contribute about 70% of this 6 ms history at
    // the worst supported shift (measured by the native impulse regression).
    // Derive frames from the actual AudioWorklet rate instead of assuming
    // 48 kHz, and keep the fallback inside the same latency budget.
    this.bufferLength = Math.max(96, Math.round(sampleRate * 0.006));
    this.buffers = [];
    this.writeIndex = 0;
    this.phase = 0;
  }

  read(buffer, position) {
    const base = Math.floor(position);
    const fraction = position - base;
    const left = buffer[(base + this.bufferLength) % this.bufferLength] || 0;
    const right = buffer[(base + 1 + this.bufferLength) % this.bufferLength] || 0;
    return left + (right - left) * fraction;
  }

  process(inputs, outputs, parameters) {
    const input = inputs[0];
    const output = outputs[0];
    if (!input?.length || !output?.length) return true;
    const ratio = Math.max(0.5, Math.min(2, Number(parameters.ratio?.[0]) || 1));
    while (this.buffers.length < output.length)
      this.buffers.push(new Float32Array(this.bufferLength));

    for (let frame = 0; frame < output[0].length; frame += 1) {
      const phaseA = (this.phase + 1) % 1;
      const phaseB = (phaseA + 0.5) % 1;
      const weightA = 0.5 - 0.5 * Math.cos(2 * Math.PI * phaseA);
      const weightB = 1 - weightA;
      for (let channel = 0; channel < output.length; channel += 1) {
        const source = input[Math.min(channel, input.length - 1)];
        const ring = this.buffers[channel];
        const sample = source?.[frame] || 0;
        ring[this.writeIndex] = sample;
        if (Math.abs(ratio - 1) < 0.0001) {
          output[channel][frame] = sample;
          continue;
        }
        const readA = this.writeIndex - phaseA * this.bufferLength;
        const readB = this.writeIndex - phaseB * this.bufferLength;
        output[channel][frame] =
          this.read(ring, readA) * weightA + this.read(ring, readB) * weightB;
      }
      this.writeIndex = (this.writeIndex + 1) % this.bufferLength;
      this.phase = (this.phase + (1 - ratio) / this.bufferLength + 1) % 1;
    }
    return true;
  }
}

registerProcessor("advoice-pitch-shift", AdVoicePitchShiftProcessor);
