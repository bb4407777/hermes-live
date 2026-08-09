// 音频层：RecorderManager 采集 16k PCM 分帧上行 + WebAudio 播放 24k 下行（turn 过滤）。
// 与 web/audio.js + player-processor.js 职责对应，API 形状尽量一致。

const FRAME_BYTES = 1024; // 512 样本 @16k PCM16 = 32ms，与服务端 VAD 帧对齐

class Recorder {
  constructor(onFrame) {
    this.onFrame = onFrame;
    this.running = false;
    this._buf = new Uint8Array(0);
    this.rm = wx.getRecorderManager();
    this.rm.onFrameRecorded(({ frameBuffer }) => {
      if (!this.running || this._paused || !frameBuffer) return;
      this._push(new Uint8Array(frameBuffer));
    });
    this.rm.onError((e) => console.error('recorder error', e));
    // 微信录音有 duration 上限，到点自动续（onStop 时若 running 且未主动暂停则重启）
    this.rm.onStop(() => { if (this.running && !this._paused) this._start(); });
  }

  // RecorderManager 的 PCM 帧长不固定，本地缓冲重切成 1024B 定长帧
  _push(chunk) {
    const merged = new Uint8Array(this._buf.length + chunk.length);
    merged.set(this._buf); merged.set(chunk, this._buf.length);
    let off = 0;
    while (merged.length - off >= FRAME_BYTES) {
      this.onFrame(merged.slice(off, off + FRAME_BYTES).buffer);
      off += FRAME_BYTES;
    }
    this._buf = merged.slice(off);
  }

  _start() {
    this.rm.start({
      format: 'PCM',
      sampleRate: 16000,
      numberOfChannels: 1,
      frameSize: 1,             // KB：每 ~1KB 回调一帧
      duration: 600000          // 单段上限 10min，onStop 自动续
    });
  }

  start() {
    return new Promise((resolve, reject) => {
      wx.authorize({
        scope: 'scope.record',
        success: () => { this.running = true; this._paused = false; this._buf = new Uint8Array(0); this._start(); resolve(); },
        fail: () => reject(new Error('未授权麦克风，请在右上角设置里打开'))
      });
    });
  }

  /** 半双工暂停：speaking 时调用，停止采集防喇叭回声进麦 */
  pause() {
    if (!this.running || this._paused) return;
    this._paused = true;
    this._buf = new Uint8Array(0);  // 清残帧
    try { this.rm.stop(); } catch (_) {}
  }

  /** 半双工恢复：listening 时调用，重新开始采集 */
  resume() {
    if (!this.running || !this._paused) return;
    this._paused = false;
    this._buf = new Uint8Array(0);
    this._start();
  }

  stop() { this.running = false; this._paused = false; try { this.rm.stop(); } catch (_) {} }
}

class Player {
  constructor(onDone) {
    this.onDone = onDone;                 // 本 turn 播完（队列排空）回调 → 发 playback_done
    this.ctx = wx.createWebAudioContext();
    this.turn = 0;
    this._sources = [];
    this._nextAt = 0;                     // 下一段的调度时间戳
    this._played = false;                 // 本 turn 是否出过声
    this._doneTimer = null;
  }

  setTurn(turn) {
    this.turn = turn;
    this._played = false;
    this._nextAt = 0;
    for (const s of this._sources) { try { s.stop(); } catch (_) {} }
    this._sources = [];
    if (this._doneTimer) { clearTimeout(this._doneTimer); this._doneTimer = null; }
  }

  // pcmBuf: ArrayBuffer，PCM16LE 24k mono
  play(turn, pcmBuf) {
    if (turn !== this.turn) return;       // 打断竞态：旧 turn 迟到帧直接丢
    const i16 = new Int16Array(pcmBuf);
    if (!i16.length) return;
    const buf = this.ctx.createBuffer(1, i16.length, 24000);
    const ch = buf.getChannelData(0);
    for (let i = 0; i < i16.length; i++) ch[i] = i16[i] / 32768;

    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.ctx.destination);
    const now = this.ctx.currentTime;
    const at = Math.max(now + 0.02, this._nextAt);   // 首段留 20ms 起播余量，后续无缝衔接
    src.start(at);
    this._nextAt = at + buf.duration;
    this._played = true;
    this._sources.push(src);
    if (this._sources.length > 64) this._sources.splice(0, 32); // 已播完的引用不必全留

    // 欠载检测：最后一段播完后 ~800ms 无新帧 → 本 turn 播放结束
    // 微信 WebAudioContext 输出缓冲比浏览器厚，350ms 会导致 playback_done 提前触发
    if (this._doneTimer) clearTimeout(this._doneTimer);
    const capturedTurn = this.turn;
    this._doneTimer = setTimeout(() => {
      if (this.turn === capturedTurn && this._played) this.onDone(capturedTurn);
    }, Math.max(0, (this._nextAt - this.ctx.currentTime) * 1000) + 800);
  }

  close() { try { this.ctx.close(); } catch (_) {} }
}

module.exports = { Recorder, Player, FRAME_BYTES };
