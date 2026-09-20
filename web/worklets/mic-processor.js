// 采集 worklet：设备采样率 → 线性重采样 16k → 512 样本 Int16 帧 postMessage。
// 只用 AudioWorklet 标准 API（Safari 14.1+ / WKWebView 兼容）。
//
// 音频线程上刻意零分配：process 每个渲染量子（128 样本，@48k 约 2.7ms）都会被调用，
// 旧实现每量子 new Float32Array + slice 一次（约 375 次/秒），GC 抖动直接表现为上行断帧。
// 现改用常驻 scratch + copyWithin，只有真正成帧（每 32ms）时才拷一次交接给主线程。

const OUT_SAMPLES = 512;      // 16k 下 32ms，与服务端约定的上行帧长
const SCRATCH = 1 << 10;      // 残留 ≤ ratio+1 个样本 + 一量子 128，容量富余

class MicProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.srcPos = 0;
    this.scratch = new Float32Array(SCRATCH);
    this.carryLen = 0;
    this.out = new Int16Array(OUT_SAMPLES);
    this.outIdx = 0;
    this.levelTick = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;

    // 电平节流：每 16 个量子一条（@48k ≈ 43ms → 约 23Hz，够动画用，不刷屏）
    if (++this.levelTick >= 16) {
      this.levelTick = 0;
      let sum = 0;
      for (let i = 0; i < ch.length; i++) sum += ch[i] * ch[i];
      this.port.postMessage({ level: Math.sqrt(sum / ch.length) });
    }

    const buf = this.scratch;
    const carry = this.carryLen;
    if (carry + ch.length > SCRATCH) {   // 理论上到不了：宁可丢帧也不越界
      this.carryLen = 0;
      this.srcPos = 0;
      return true;
    }
    buf.set(ch, carry);
    const n = carry + ch.length;

    let pos = this.srcPos;
    while (pos + 1 < n) {
      const i = pos | 0;
      const frac = pos - i;
      const s = buf[i] + (buf[i + 1] - buf[i]) * frac;
      this.out[this.outIdx++] = s < -1 ? -32768 : (s > 0.99997 ? 32767 : (s * 32768) | 0);
      if (this.outIdx === OUT_SAMPLES) {
        const copy = this.out.slice(0);
        this.port.postMessage(copy.buffer, [copy.buffer]);
        this.outIdx = 0;
      }
      pos += this.ratio;
    }

    // pos 会越过 n 最多 ratio 个样本（48k 时 3），keep 一旦 >n 就变成负 carryLen，
    // 下一量子 buf.set(ch, -1) 直接 RangeError —— 音频线程抛异常等于麦克风彻底哑掉。
    // 至少要留下最后一个样本给下次插值当右端点。
    const keep = Math.min(pos | 0, n - 1);
    buf.copyWithin(0, keep, n);
    this.carryLen = n - keep;
    this.srcPos = pos - keep;
    return true;
  }
}

registerProcessor('mic-processor', MicProcessor);
