// 播放 worklet：24k PCM16 环形队列 → 线性重采样到设备采样率。
// turn 过滤解决打断竞态（仿 moshi audio-processor 的缓冲思路）：
//   setTurn → 清缓冲 + 10ms 淡出防爆音；audio 帧 turn 不符直接丢。
// 收口协议：服务端每 turn 音频发完会发 tts_end → 主线程转成 {type:'end'}。
//   只有「已收到 end 且持续欠载 ~300ms」才 post done。此前靠纯欠载猜测，
//   而 edge-tts 逐句合成句间空隙轻松 >300ms，会被误判成播完 →
//   服务端提前回 listening、喇叭还在播 → 自己的 TTS 被录进下一轮。
// 预灌水位 PREFILL_MS：起播与每次欠载恢复都先攒够再出声。服务端首帧音频和
//   state=speaking 同批下发，不设水位就是「缓冲 0ms 开播」，出站队列任何抖动
//   都会当场掏空，形成词内缝隙。

const PREFILL_MS = 120;

class PlayerProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = 24000 / sampleRate;
    this.prefillSamples = (24000 * PREFILL_MS) / 1000;
    this.fadeSamples = Math.max(1, Math.round(sampleRate * 0.01));  // 10ms 淡出
    // 300ms 持续欠载才算播完，换算成 process 调用次数（每调用 128 帧）
    this.doneCalls = Math.max(1, Math.round(0.3 / (128 / sampleRate)));
    this.chunks = [];        // Float32Array 队列（24k）
    this.queued = 0;         // 队列内可用样本数（24k 域，含小数）
    this.pos = 0;            // 当前 chunk 内的小数读位置
    this.turn = -1;
    this.started = false;    // 预灌达标后才出声
    this.ended = false;      // 本 turn 音频服务端已发完
    this.playedAny = false;
    this.notifiedDone = false;
    this.silentCalls = 0;    // 连续欠载的 process 调用数（128 帧 ≈ 2.7ms @48k）
    this.fade = 0;
    this.last = 0;

    this.port.onmessage = (e) => {
      const d = e.data;
      if (d.type === 'audio') {
        if (d.turn !== this.turn) return;
        // offset 缺省必须按 0 处理：主线程有两种形态（0.4.6 传预切掉 2 字节头的独立缓冲，
        // 0.4.7 起传整块下行 ArrayBuffer + offset）。写成 d.pcm.byteLength - d.offset 时，
        // 一旦浏览器缓存的 app.js 与新 worklet 不同批（静态资源没有 Cache-Control，Safari
        // 会按启发式新鲜度各存各的），undefined 会让长度算成 NaN→>>1→0，每块都是空块 →
        // 全程静音，而麦克风/ASR 一切正常，页面看不出任何异常。
        const off = d.offset | 0;
        const n = (d.pcm.byteLength - off) >> 1;
        if (n <= 0) return;
        // pcm 可能带协议头（前 2 字节），按 offset 建视图，避免主线程为每个下行块再 copy 一次
        const i16 = new Int16Array(d.pcm, off, n);
        const f32 = new Float32Array(i16.length);
        for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
        this.chunks.push(f32);
        this.queued += f32.length;
        this.notifiedDone = false;
        this.silentCalls = 0;
      } else if (d.type === 'setTurn') {
        // 下行音频帧里的 turn 是 1 字节（协议头 0x01 + turn(u8) + PCM），而 JSON 事件带的是
        // 完整整数：不取到低 8 位，第 256 轮起两边永不相等 → 音频全被当旧 turn 丢掉（静默）。
        const t = d.turn & 0xFF;
        // 同一 turn 重复 setTurn 不再清缓冲：主线程的 state 事件与 clear() 都会发这一条，
        // 连带抹掉已在队列里的音频就会把整轮回答啃成碎片（实测 960ms 只剩 46ms 有声）。
        // 真要丢缓冲走下面的 'clear'。
        if (t !== this.turn) {
          this.turn = t;
          this._wipe();
        }
      } else if (d.type === 'clear') {
        this._wipe();   // 只丢缓冲、保持 turn：新会话/重连后旧会话残留不该继续播
      } else if (d.type === 'end') {
        if ((d.turn & 0xFF) === this.turn) this.ended = true;
      }
    };
  }

  _wipe() {
    this.chunks = [];
    this.queued = 0;
    this.pos = 0;
    this.started = false;
    this.ended = false;
    this.playedAny = false;
    this.notifiedDone = false;
    this.silentCalls = 0;
    this.fade = this.fadeSamples;   // ~10ms @48k：残余样本淡出
  }

  _next() {
    // 先规范化读位置：跳过读完的块。用 while 是因为尾块可能比一次跳的 ratio 还短
    // （低采样率设备 ratio>1，越界的 cur[i] 是 undefined → 一路 NaN 出去就是噪音）
    while (this.chunks.length && this.pos >= this.chunks[0].length) {
      this.pos -= this.chunks[0].length;
      this.chunks.shift();
    }
    if (this.chunks.length === 0) return null;
    const cur = this.chunks[0];
    const i = Math.floor(this.pos);
    const frac = this.pos - i;
    const a = cur[i];
    // 跨 chunk 边界取下一块首样本插值；用「重复末样本」填充会在每块交界处留下台阶
    const next = this.chunks[1];
    const b = i + 1 < cur.length ? cur[i + 1] : (next && next.length ? next[0] : a);
    this.pos += this.ratio;
    this.queued -= this.ratio;
    return a * (1 - frac) + b * frac;
  }

  _idle() {
    // 欠载计数：预灌等待期也算，否则结尾那 300ms 永远熬不完（started 一被清零
    // 就早返回，silentCalls 冻在 1，done 再不上报，服务端只能靠超时兜底）
    this.silentCalls++;
    if (this.ended && this.playedAny && !this.notifiedDone
        && this.silentCalls >= this.doneCalls) {
      this.notifiedDone = true;
      this.port.postMessage({ type: 'done', turn: this.turn });
    }
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    if (!this.started) {
      // 攒够水位再开播；本 turn 已发完且只剩这点儿，就别等了（否则结尾永远播不出）
      if (this.queued < this.prefillSamples && !(this.ended && this.queued > 0)) {
        this._idle();
        return true;
      }
      this.started = true;
    }
    let underrun = true;
    for (let i = 0; i < out.length; i++) {
      let s = this._next();
      if (s === null) {
        // 欠载：从最后样本淡出到 0
        s = this.fade > 0 ? this.last * (this.fade / this.fadeSamples) : 0;
        if (this.fade > 0) this.fade--;
      } else {
        underrun = false;
        this.playedAny = true;
        this.fade = this.fadeSamples;
      }
      this.last = s;
      out[i] = s;
    }
    if (underrun) {
      this.started = false;      // 下一批样本要重新预灌，别挤出一两个样本就播
      // 走到这里 chunks 必已排空（_next 只在队列空时返回 null），写死 0 顺带校正
      // 每量子扣减 ratio 攒下的小数误差。
      this.queued = 0;
      this._idle();
    } else {
      this.silentCalls = 0;
    }
    return true;
  }
}

registerProcessor('player-processor', PlayerProcessor);
