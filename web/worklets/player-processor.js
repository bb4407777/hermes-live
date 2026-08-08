// 播放 worklet：24k PCM16 环形队列 → 线性重采样到设备采样率。
// turn 过滤解决打断竞态（仿 moshi audio-processor 的缓冲思路）：
//   setTurn → 清缓冲 + 10ms 淡出防爆音；audio 帧 turn 不符直接丢。
// 持续欠载 ~300ms 且本 turn 播过声 → postMessage done（app 转发 playback_done）。

class PlayerProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = 24000 / sampleRate;
    this.chunks = [];        // Float32Array 队列（24k）
    this.pos = 0;            // 当前 chunk 内的小数读位置
    this.turn = -1;
    this.playedAny = false;
    this.notifiedDone = false;
    this.silentCalls = 0;    // 连续欠载的 process 调用数（128 帧 ≈ 2.7ms @48k）
    this.fade = 0;
    this.last = 0;

    this.port.onmessage = (e) => {
      const d = e.data;
      if (d.type === 'audio') {
        if (d.turn !== this.turn) return;
        const i16 = new Int16Array(d.pcm);
        const f32 = new Float32Array(i16.length);
        for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
        this.chunks.push(f32);
        this.notifiedDone = false;
        this.silentCalls = 0;
      } else if (d.type === 'setTurn') {
        this.turn = d.turn;
        this.chunks = [];
        this.pos = 0;
        this.playedAny = false;
        this.notifiedDone = false;
        this.silentCalls = 0;
        this.fade = 480;     // ~10ms @48k：残余样本淡出
      }
    };
  }

  _next() {
    if (this.chunks.length === 0) return null;
    const cur = this.chunks[0];
    const i = Math.floor(this.pos);
    const frac = this.pos - i;
    const a = cur[i];
    const b = i + 1 < cur.length ? cur[i + 1] : a;
    this.pos += this.ratio;
    if (this.pos >= cur.length) {
      this.pos -= cur.length;
      this.chunks.shift();
    }
    return a * (1 - frac) + b * frac;
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    let underrun = true;
    for (let i = 0; i < out.length; i++) {
      let s = this._next();
      if (s === null) {
        // 欠载：从最后样本淡出到 0
        s = this.fade > 0 ? this.last * (this.fade / 480) : 0;
        if (this.fade > 0) this.fade--;
      } else {
        underrun = false;
        this.playedAny = true;
        this.fade = 480;
      }
      this.last = s;
      out[i] = s;
    }
    if (underrun && this.playedAny && !this.notifiedDone) {
      if (++this.silentCalls >= 112) {   // ≈300ms 持续欠载
        this.notifiedDone = true;
        this.port.postMessage({ type: 'done', turn: this.turn });
      }
    }
    return true;
  }
}

registerProcessor('player-processor', PlayerProcessor);
