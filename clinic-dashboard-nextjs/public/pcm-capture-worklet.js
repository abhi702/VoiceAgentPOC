class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.remaining = this.ratio;
    this.sum = 0;
    this.chunk = new Int16Array(320);
    this.index = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) return true;
    for (const value of channel) {
      let weight = 1;
      while (weight > 1e-9) {
        const part = Math.min(weight, this.remaining);
        this.sum += value * part;
        this.remaining -= part;
        weight -= part;
        if (this.remaining < 1e-9) {
          const sample = Math.max(-1, Math.min(1, this.sum / this.ratio));
          this.chunk[this.index++] = Math.round(sample * (sample < 0 ? 32768 : 32767));
          this.sum = 0;
          this.remaining = this.ratio;
          if (this.index === this.chunk.length) {
            this.port.postMessage(this.chunk.buffer, [this.chunk.buffer]);
            this.chunk = new Int16Array(320);
            this.index = 0;
          }
        }
      }
    }
    return true;
  }
}

registerProcessor("pcm-capture", PcmCapture);
