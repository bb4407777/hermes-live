// 音频装配：getUserMedia + 两个 worklet。只用 Safari/WKWebView 兼容 API：
// getUserMedia / AudioContext（不传 sampleRate，WebKit 行为不一）/ AudioWorklet / WebSocket。

export class AudioIO {
  constructor({ onFrame, onLevel, onPlaybackDone }) {
    this.onFrame = onFrame;
    this.onLevel = onLevel;
    this.onPlaybackDone = onPlaybackDone;
    this.ctx = null;
    this.stream = null;
    this._turn = -1;
    this._micPaused = false;
    // ── 2026-09-20 无声诊断：下行帧数、worklet 已播帧数、ctx 状态 ──
    this._dbgPlayed = 0;
    this._dbgSent = 0;
  }

  dbg() {
    const st = this.ctx ? this.ctx.state : '无ctx';
    return `cv${st} P${this._dbgSent} R${this._dbgPlayed}`;
  }

  // 必须看 playerNode 与 ctx 状态：旧写法 !!this.ctx 在 addModule 失败时也已为真，
  // 于是 app 侧 `if (!audio.running)` 永不再试、playAudio 静默 no-op（表现为
  // 「圆球亮着、麦克风开着、什么声音都没有」）
  get running() {
    return !!this.playerNode && !!this.ctx && this.ctx.state !== 'closed';
  }

  // speaking 态暂停发帧（半双工，防回声）；listening 态恢复
  setMicPaused(paused) {
    this._micPaused = paused;
  }

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    try {
      const ctx = new AudioContext();
      await ctx.resume();
      // 2026-09-20 0.4.8 回归加固：worklet 走 addModule，Safari 对 module 的启发式缓存比
      // 普通资源顽固（no-cache 头只在回源时生效，已装模块可直接复用旧实例）。URL 带版本号
      // = 版本一变就是新 URL，强制重取，用户无需清缓存。改 __version__ 时同步这里。
      const WORKLET_V = '0.4.8';
      await ctx.audioWorklet.addModule('/web/worklets/mic-processor.js?v=' + WORKLET_V);
      await ctx.audioWorklet.addModule('/web/worklets/player-processor.js?v=' + WORKLET_V);
      this.ctx = ctx;                       // 全部就绪后再暴露，避免 running 早判

      this.src = ctx.createMediaStreamSource(this.stream);
      this.micNode = new AudioWorkletNode(ctx, 'mic-processor');
      this.playerNode = new AudioWorkletNode(ctx, 'player-processor', {
        outputChannelCount: [1],
      });

      this.micNode.port.onmessage = (e) => {
        if (e.data && e.data.level !== undefined) this.onLevel(e.data.level);
        else if (!this._micPaused) this.onFrame(e.data); // 半双工：speaking 态不发帧
      };
      this.playerNode.port.onmessage = (e) => {
        if (e.data && e.data.type === 'done') {
          this._dbgPlayed++;
          this.onPlaybackDone(e.data.turn);
        }
      };

      // mic worklet 需接入图中才会被驱动：经 0 增益静音节点到输出
      this.mute = ctx.createGain();
      this.mute.gain.value = 0;
      this.src.connect(this.micNode);
      this.micNode.connect(this.mute).connect(ctx.destination);
      this.playerNode.connect(ctx.destination);
    } catch (err) {
      // 半途失败要把已开的麦克风流和 ctx 收干净，否则授权指示灯常亮且无法重试
      await this.stop();
      throw err;
    }
  }

  // iOS/Safari 后台回来后 ctx 常被挂起：此时 worklet 不被驱动，采集与播放全冻结
  resumeIfNeeded() {
    if (this.ctx && this.ctx.state === 'suspended') return this.ctx.resume().catch(() => {});
    return Promise.resolve();
  }

  // buf 是整块下行 ArrayBuffer（前 2 字节协议头），转移原缓冲而非复制一份：
  // 每个下行块一次分配在音频线程上是实打实的 GC 压力
  playAudio(turn, buf, offset) {
    this._dbgSent++;
    if (this.playerNode) {
      this.playerNode.port.postMessage({ type: 'audio', turn, pcm: buf, offset }, [buf]);
    }
  }

  setTurn(turn) {
    this._turn = turn;
    if (this.playerNode) this.playerNode.port.postMessage({ type: 'setTurn', turn });
  }

  setTurnEnd(turn) {
    if (this.playerNode) this.playerNode.port.postMessage({ type: 'end', turn });
  }

  // 丢弃已排队音频但保持当前 turn（新会话/重连后用，避免残留继续播）。
  // 不能走 setTurn：主线程 setTurn 对同一 turn 已改成幂等（不清缓冲），且这里
  // _turn 可能是初始值 -1，& 0xFF 后变成 255，反而把 worklet 的 turn 弄错。
  clear() {
    if (this.playerNode) this.playerNode.port.postMessage({ type: 'clear' });
  }

  async stop() {
    if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
    if (this.ctx) await this.ctx.close().catch(() => {});
    this.ctx = null;
    this.stream = null;
    this.playerNode = null;
    this.micNode = null;
    this._turn = -1;
  }
}
