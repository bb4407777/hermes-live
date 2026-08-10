const { Recorder, Player } = require('../../utils/audio.js');

const STATE_LABEL = { idle: '待机', listening: '聆听中', thinking: '思考中', speaking: '说话中' };

const VOICES = [
  { label: '晓晓（女）', value: 'zh-CN-XiaoxiaoNeural' },
  { label: '云希（男）', value: 'zh-CN-YunxiNeural' },
  { label: '晓伊（女）', value: 'zh-CN-XiaoyiNeural' },
  { label: '云健（男）', value: 'zh-CN-YunjianNeural' },
];
const RATES = [
  { label: '1.0x', value: '+0%' },
  { label: '1.15x', value: '+15%' },
  { label: '1.3x', value: '+30%' },
  { label: '0.85x', value: '-15%' },
];

Page({
  data: {
    server: '', token: '',
    connected: false, running: false,
    state: 'idle', stateLabel: '待机',
    canInterrupt: false,
    partialText: '',
    messages: [],
    textIn: '',
    scrollInto: '',
    showSetup: false,
    voiceIdx: 0, voiceLabels: VOICES.map(v => v.label),
    rateIdx: 0,  rateLabels: RATES.map(r => r.label),
  },
  _ws: null, _rec: null, _player: null,
  _agentMsgId: null, _agentTurn: -1, _msgSeq: 0,
  _connecting: false,

  onLoad() {
    const app = getApp();
    this.setData({
      server: app.globalData.server,
      token:  app.globalData.token,
      showSetup: !app.globalData.server,
    });
    this._player = new Player((turn) => this._sendJson({ type: 'playback_done', turn }));
    this._initRec();
  },

  onShow() {
    // 每次进入页面自动开聊（已授权麦克风后静默开始，未授权则弹系统授权弹窗）
    if (!this.data.running && !this._connecting && !this.data.showSetup) {
      this._autoStart();
    }
  },

  onUnload() {
    this._stopAll();
    if (this._player) this._player.close();
  },

  // ---------- 自动开始 ----------
  async _autoStart() {
    this._connecting = true;
    try {
      if (!this._ws || !this.data.connected) await this._connect();
      await this._rec.start();
      this._sendJson({ type: 'start' });
      this.setData({ running: true });
    } catch (e) {
      // 静默失败——用户可手动点「开始对话」
    } finally {
      this._connecting = false;
    }
  },

  _initRec() {
    this._rec = new Recorder((frame) => {
      if (!this._ws) return;
      const out = new Uint8Array(1 + frame.byteLength);
      out[0] = 0x01; out.set(new Uint8Array(frame), 1);
      this._ws.send({ data: out.buffer });
    });
  },

  // ---------- WS 连接 ----------
  _wsUrl() {
    let s = this.data.server.trim();
    if (!/^wss?:\/\//.test(s)) s = 'ws://' + s;
    return `${s}/ws${this.data.token ? '?token=' + encodeURIComponent(this.data.token) : ''}`;
  },

  _connect() {
    const url = this._wsUrl();
    return new Promise((resolve, reject) => {
      const ws = wx.connectSocket({ url });
      let opened = false;
      ws.onOpen(() => { opened = true; this._ws = ws; this.setData({ connected: true }); resolve(); });
      ws.onError((e) => { if (!opened) reject(new Error(e.errMsg || url)); });
      ws.onClose(({ code, reason }) => {
        this._ws = null;
        this.setData({ connected: false, running: false, state: 'idle', stateLabel: STATE_LABEL.idle, canInterrupt: false });
        if (this._rec) this._rec.stop();
        if (opened && code !== 1000) this._addMsg('error', `连接断开(${code})${reason ? '：' + reason : ''}`);
      });
      ws.onMessage(({ data }) => {
        if (typeof data === 'string') this._handleJson(JSON.parse(data));
        else this._handleBinary(data);
      });
    });
  },

  _sendJson(obj) { if (this._ws) this._ws.send({ data: JSON.stringify(obj) }); },

  _handleBinary(buf) {
    const v = new Uint8Array(buf);
    if (v[0] === 0x01) this._player.play(v[1], buf.slice(2));
  },

  _handleJson(msg) {
    switch (msg.type) {
      case 'hello':
        if (msg.note) this._addMsg('tool', msg.note);
        break;
      case 'state': {
        const st = msg.state;
        this.setData({ state: st, stateLabel: STATE_LABEL[st] || st, canInterrupt: false });
        if (msg.turn !== undefined) this._player.setTurn(msg.turn);
        // 半双工：speaking 时停录（防喇叭回声进麦），listening 时恢复
        if (st !== 'listening') this.setData({ partialText: '' });
        if (st === 'speaking') {
          this._rec && this._rec.pause();
        } else if (st === 'listening') {
          // 延迟 500ms 再开录：等喇叭输出缓冲彻底排空，防末尾音频录进麦克风
          // （微信 WebAudio 输出延迟比浏览器大，立刻开录必然回声）
          setTimeout(() => {
            if (this.data.state === 'listening' && this._rec) this._rec.resume();
          }, 500);
        }
        break;
      }
      case 'tts_end':
        this._player.endTurn();
        break;
      case 'asr_partial':
        // 更新实时字幕，同时滚动到底部让 partial 气泡保持可见
        this.setData({ partialText: msg.text, scrollInto: 'partial-bubble' });
        break;
      case 'asr_final':
        this.setData({ partialText: '' });
        this._addMsg('user', msg.text);
        break;
      case 'agent_delta': {
        if (this._agentTurn !== msg.turn || this._agentMsgId === null) {
          this._agentMsgId = this._addMsg('agent', '');
          this._agentTurn = msg.turn;
        }
        const list = this.data.messages;
        const m = list.find(x => x.id === this._agentMsgId);
        if (m) { m.text += msg.text; this._refreshMsgs(list); }
        break;
      }
      case 'agent_done': this._agentMsgId = null; break;
      case 'error': this._addMsg('error', msg.message); break;
    }
  },

  _addMsg(cls, text) {
    const id = ++this._msgSeq;
    const list = this.data.messages.concat([{ id, cls, text }]);
    if (list.length > 200) list.splice(0, list.length - 200);
    this._refreshMsgs(list);
    return id;
  },

  _refreshMsgs(list) {
    this.setData({ messages: list, scrollInto: 'm' + this._msgSeq });
  },

  // ---------- 语音开关 ----------
  async toggle() {
    if (this.data.running) { this._stopAll(); return; }
    this._connecting = true;
    try {
      if (!this._ws || !this.data.connected) await this._connect();
      await this._rec.start();
      this._sendJson({ type: 'start' });
      this.setData({ running: true });
    } catch (e) {
      this._addMsg('error', '启动失败：' + (e.message || e));
    } finally { this._connecting = false; }
  },

  _stopAll() {
    this._sendJson({ type: 'stop' });
    if (this._rec) this._rec.stop();
    this.setData({ running: false, state: 'idle', stateLabel: STATE_LABEL.idle, canInterrupt: false });
  },

  interrupt() { this._sendJson({ type: 'interrupt' }); },

  newSession() {
    this._sendJson({ type: 'new_session' });
    this._agentMsgId = null;
    this.setData({ messages: [] });
  },

  // ---------- 音色 / 语速 ----------
  pushConfig() {
    this._sendJson({
      type: 'set_config',
      voice: VOICES[this.data.voiceIdx].value,
      tts_rate: RATES[this.data.rateIdx].value,
    });
  },
  onVoiceChange(e) { this.setData({ voiceIdx: +e.detail.value }); this.pushConfig(); },
  onRateChange(e)  { this.setData({ rateIdx:  +e.detail.value }); this.pushConfig(); },

  // ---------- 文字输入 ----------
  onInput(e) { this.setData({ textIn: e.detail.value }); },

  async sendText() {
    const text = this.data.textIn.trim();
    if (!text) return;
    this.setData({ textIn: '' });
    this._addMsg('user', text);
    try {
      if (!this._ws || !this.data.connected) await this._connect();
      this._sendJson({ type: 'text', text });
    } catch (e) { this._addMsg('error', e.message || String(e)); }
  },

  // ---------- 设置 ----------
  openSetup() { this.setData({ showSetup: true }); },
  onServerInput(e) { this.setData({ server: e.detail.value }); },
  onTokenInput(e)  { this.setData({ token: e.detail.value }); },
  saveSetup() {
    const app = getApp();
    app.globalData.server = this.data.server.trim();
    app.globalData.token  = this.data.token.trim();
    wx.setStorageSync('hl_server', app.globalData.server);
    wx.setStorageSync('hl_token',  app.globalData.token);
    if (this._ws) { try { this._ws.close(); } catch (_) {} this._ws = null; }
    this.setData({ showSetup: false });
    // 保存后自动重连
    if (!this.data.running) setTimeout(() => this._autoStart(), 300);
  },
  cancelSetup() { this.setData({ showSetup: false }); },
  noop() {},
});
