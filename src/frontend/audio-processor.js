/**
 * AudioWorklet Processor for evilSDR
 * Receives Float32 PCM chunks via message port, buffers them in a ring buffer,
 * and outputs to the audio graph.
 */
class SDRAudioProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    // Ring buffer: ~5 seconds at 48kHz
    this.bufferSize = 48000 * 5;
    this.buffer = new Float32Array(this.bufferSize);
    this.writePos = 0;
    this.readPos = 0;
    this.buffered = 0;
    // Start playback after accumulating this many samples (latency vs stability)
    this.minBuffer = 9600; // 200ms — increased for stability on threaded backend
    this.playing = false; // gate: only start after minBuffer, don't stop until empty

    this.port.onmessage = (e) => {
      if (e.data instanceof Float32Array) {
        this._write(e.data);
      } else if (e.data === 'CLEAR') {
        this.writePos = 0;
        this.readPos = 0;
        this.buffered = 0;
        this.playing = false;
        this.buffer.fill(0);
      }
    };
  }

  _write(samples) {
    for (let i = 0; i < samples.length; i++) {
      this.buffer[this.writePos] = samples[i];
      this.writePos = (this.writePos + 1) % this.bufferSize;
    }
    this.buffered += samples.length;
    if (this.buffered > this.bufferSize) {
      // Overflow - reset to avoid stale data
      this.buffered = this.bufferSize;
      this.readPos = (this.writePos - this.bufferSize + this.bufferSize) % this.bufferSize;
    }
  }

  process(inputs, outputs, parameters) {
    const output = outputs[0];
    const channel = output[0]; // mono
    if (!channel) return true;

    // Gated playback: wait for minBuffer before starting, then play until empty
    if (!this.playing) {
      if (this.buffered >= this.minBuffer) {
        this.playing = true;
      } else {
        channel.fill(0);
        return true;
      }
    }

    // If running low (but not empty), keep playing — avoid re-gate thrashing
    if (this.buffered <= 0) {
      // Fully dry: fade to silence but DON'T re-gate immediately.
      // Use a short grace period to avoid underrun/overrun cycle.
      if (!this._drySince) this._drySince = currentFrame;
      // Grace: ~50ms (2400 frames at 48kHz) before re-gating
      if (currentFrame - this._drySince > 2400) {
        this.playing = false;
        this._drySince = 0;
      }
      channel.fill(0);
      return true;
    }
    this._drySince = 0;

    // Catch up if buffer is too large (more than 500ms lag) — gentler than before
    if (this.buffered > 24000) {
       // Skip to leave ~200ms buffered
       const skip = this.buffered - 9600;
       this.readPos = (this.readPos + skip) % this.bufferSize;
       this.buffered = 9600;
    }

    for (let i = 0; i < channel.length; i++) {
      if (this.buffered > 0) {
        channel[i] = this.buffer[this.readPos];
        this.readPos = (this.readPos + 1) % this.bufferSize;
        this.buffered--;
      } else {
        channel[i] = 0;
      }
    }
    return true;
  }
}

registerProcessor('sdr-audio-processor', SDRAudioProcessor);
