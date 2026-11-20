export class PhoneAudio {
  private readonly context = new AudioContext();
  private stream?: MediaStream;
  private source?: MediaStreamAudioSourceNode;
  private capture?: AudioWorkletNode;
  private mute?: GainNode;
  private analyser?: AnalyserNode;
  private animation = 0;
  private endTimer?: ReturnType<typeof setTimeout>;
  private readonly sources = new Set<AudioBufferSourceNode>();
  private nextPlayback = 0;
  private closed = false;

  async prepare(): Promise<void> { await this.context.resume(); }

  async startMicrophone(onPcm: (pcm: ArrayBuffer) => void, onLevel: (levels: number[]) => void): Promise<void> {
    await this.context.audioWorklet.addModule("/pcm-capture-worklet.js");
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    if (this.closed) {
      this.stream.getTracks().forEach((track) => track.stop());
      return;
    }
    this.source = this.context.createMediaStreamSource(this.stream);
    this.capture = new AudioWorkletNode(this.context, "pcm-capture");
    this.capture.port.onmessage = (event: MessageEvent<ArrayBuffer>) => onPcm(event.data);
    this.mute = this.context.createGain();
    this.mute.gain.value = 0;
    this.source.connect(this.capture).connect(this.mute).connect(this.context.destination);
    this.analyser = this.context.createAnalyser();
    this.analyser.fftSize = 256;
    this.source.connect(this.analyser);
    const values = new Uint8Array(this.analyser.frequencyBinCount);
    const tick = (): void => {
      if (!this.analyser || this.closed) return;
      this.analyser.getByteFrequencyData(values);
      onLevel(Array.from({ length: 24 }, (_, i) => values[i * 3] / 255));
      this.animation = requestAnimationFrame(tick);
    };
    tick();
  }

  play(pcm: ArrayBuffer): void {
    if (this.closed || pcm.byteLength % 2) return;
    const view = new DataView(pcm);
    const samples = new Float32Array(pcm.byteLength / 2);
    for (let i = 0; i < samples.length; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
    const buffer = this.context.createBuffer(1, samples.length, 16000);
    buffer.copyToChannel(samples, 0);
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);
    const start = Math.max(this.context.currentTime + 0.025, this.nextPlayback);
    this.nextPlayback = start + buffer.duration;
    this.sources.add(source);
    source.onended = () => { this.sources.delete(source); source.disconnect(); };
    source.start(start);
  }

  afterPlayback(callback: () => void): void {
    clearTimeout(this.endTimer);
    this.endTimer = setTimeout(callback, Math.max(0, this.nextPlayback - this.context.currentTime) * 1000 + 100);
  }

  interrupt(): void {
    clearTimeout(this.endTimer);
    for (const source of this.sources) source.stop();
    this.sources.clear();
    this.nextPlayback = this.context.currentTime;
  }

  async close(): Promise<void> {
    this.closed = true;
    this.interrupt();
    cancelAnimationFrame(this.animation);
    this.stream?.getTracks().forEach((track) => track.stop());
    this.source?.disconnect();
    this.capture?.disconnect();
    this.mute?.disconnect();
    await this.context.close();
  }
}
