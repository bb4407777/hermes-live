// 采集 worklet：设备采样率 → 线性重采样 16k → 512 样本 Int16 帧 postMessage。
// 只用 AudioWorklet 标准 API（Safari 14.1+ / WKWebView 兼容）。

class MicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.srcPos = 0;
    this.carry = new Float32Array(0);
    this.out = new Int16Array(512);
    this.outIdx = 0;
    this.levelTick = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;

    // 电平（节流 ~8Hz）供状态球动画
    if (++this.levelTick >= 16) {
      this.levelTick = 0;
      let sum = 0;
      for (let i = 0; i < ch.length; i++) sum += ch[i] * ch[i];
      this.port.postMessage({ level: Math.sqrt(sum / ch.length) });
    }

    const buf = new Float32Array(this.carry.length + ch.length);
    buf.set(this.carry);
    buf.set(ch, this.carry.length);

    let pos = this.srcPos;
    while (pos + 1 < buf.length) {
      const i = Math.floor(pos);
      const frac = pos - i;
      const s = buf[i] * (1 - frac) + buf[i + 1] * frac;
      this.out[this.outIdx++] = Math.max(-32768, Math.min(32767, (s * 32768) | 0));
      if (this.outIdx === 512) {
        const copy = this.out.slice(0);
        this.port.postMessage(copy.buffer, [copy.buffer]);
        this.outIdx = 0;
      }
      pos += this.ratio;
    }
    const keep = Math.floor(pos);
    this.carry = buf.slice(keep);
    this.srcPos = pos - keep;
    return true;
  }
}

registerProcessor('mic-processor', MicProcessor);
