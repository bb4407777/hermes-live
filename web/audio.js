// 音频装配：getUserMedia + 两个 worklet。只用 Safari/WKWebView 兼容 API：
// getUserMedia / AudioContext（不传 sampleRate，WebKit 行为不一）/ AudioWorklet / WebSocket。

export class AudioIO {
  constructor({ onFrame, onLevel, onPlaybackDone }) {
    this.onFrame = onFrame;
    this.onLevel = onLevel;
    this.onPlaybackDone = onPlaybackDone;
    this.ctx = null;
    this.stream = null;
  }

  get running() {
    return !!this.ctx;
  }

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    this.ctx = new AudioContext();
    await this.ctx.resume();
    await this.ctx.audioWorklet.addModule('/web/worklets/mic-processor.js');
    await this.ctx.audioWorklet.addModule('/web/worklets/player-processor.js');

    this.src = this.ctx.createMediaStreamSource(this.stream);
    this.micNode = new AudioWorkletNode(this.ctx, 'mic-processor');
    this.playerNode = new AudioWorkletNode(this.ctx, 'player-processor', {
      outputChannelCount: [1],
    });

    this.micNode.port.onmessage = (e) => {
      if (e.data && e.data.level !== undefined) this.onLevel(e.data.level);
      else this.onFrame(e.data); // ArrayBuffer：1024 字节 PCM16 @16k
    };
    this.playerNode.port.onmessage = (e) => {
      if (e.data && e.data.type === 'done') this.onPlaybackDone(e.data.turn);
    };

    // mic worklet 需接入图中才会被驱动：经 0 增益静音节点到输出
    this.mute = this.ctx.createGain();
    this.mute.gain.value = 0;
    this.src.connect(this.micNode);
    this.micNode.connect(this.mute).connect(this.ctx.destination);
    this.playerNode.connect(this.ctx.destination);
  }

  playAudio(turn, pcmBuffer) {
    if (this.playerNode) this.playerNode.port.postMessage({ type: 'audio', turn, pcm: pcmBuffer }, [pcmBuffer]);
  }

  setTurn(turn) {
    if (this.playerNode) this.playerNode.port.postMessage({ type: 'setTurn', turn });
  }

  async stop() {
    if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
    if (this.ctx) await this.ctx.close().catch(() => {});
    this.ctx = null;
    this.stream = null;
    this.playerNode = null;
    this.micNode = null;
  }
}
