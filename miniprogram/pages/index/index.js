// 主页面：状态机显示 + 对话流 + 语音/文字双输入。协议处理与 web/app.js 对齐。
const { Recorder, Player } = require('../../utils/audio.js');

const STATE_LABEL = { idle: '未开始', listening: '聆听中', thinking: '思考中', speaking: '说话中' };

Page({
  data: {
    server: '', token: '',
    connected: false, running: false,
    state: 'idle', stateLabel: '未开始',
    canInterrupt: false,
    messages: [],            // {id, cls: 'user'|'agent'|'tool'|'error', text}
    textIn: '',
    scrollInto: '',
    showSetup: false
  },
  _ws: null, _rec: null, _player: null,
  _agentMsgId: null, _agentTurn: -1, _msgSeq: 0,

  onLoad() {
    const app = getApp();
    this.setData({
      server: app.globalData.server,
      token: app.globalData.token,
      showSetup: !app.globalData.server
    });
    this._player = new Player((turn) => this._sendJson({ type: 'playback_done', turn }));
  },

  onUnload() {
    this._stopAll();
    if (this._player) this._player.close();
  },

  // ---------- 连接 ----------
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
      ws.onError((e) => {
        if (!opened) reject(new Error('连接失败：' + url + '\n' + (e.errMsg || '')));
      });
      ws.onClose(({ code, reason }) => {
        this._ws = null;
        this.setData({ connected: false, running: false, state: 'idle', stateLabel: STATE_LABEL.idle });
        if (this._rec) this._rec.stop();
        if (opened && code !== 1000) this._addMsg('error', `连接断开(${code})${reason ? '：'+reason : ''}`);
      });
      ws.onMessage(({ data }) => {
        if (typeof data === 'string') this._handleJson(JSON.parse(data));
        else this._handleBinary(data);
      });
    });
  },

  _sendJson(obj) { if (this._ws) this._ws.send({ data: JSON.stringify(obj) }); },

  _handleBinary(buf) {
    const view = new Uint8Array(buf);
    if (view[0] === 0x01) this._player.play(view[1], buf.slice(2));
  },

  _handleJson(msg) {
    switch (msg.type) {
      case 'hello':
        if (msg.note) this._addMsg('tool', msg.note);
        break;
      case 'state': {
        const st = msg.state;
        this.setData({
          state: st, stateLabel: STATE_LABEL[st] || st,
          canInterrupt: st === 'thinking' || st === 'speaking'
        });
        if (msg.turn !== undefined) this._player.setTurn(msg.turn);
        break;
      }
      case 'asr_final':
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
      case 'agent_done':
        this._agentMsgId = null;
        break;
      case 'error':
        this._addMsg('error', msg.message);
        break;
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

  // ---------- 语音 ----------
  async toggle() {
    if (this.data.running) { this._stopAll(); return; }
    try {
      if (!this._ws) await this._connect();
      if (!this._rec) this._rec = new Recorder((frame) => {
        if (!this._ws) return;
        const out = new Uint8Array(1 + frame.byteLength);
        out[0] = 0x01; out.set(new Uint8Array(frame), 1);
        this._ws.send({ data: out.buffer });
      });
      await this._rec.start();
      this._sendJson({ type: 'start' });
      this.setData({ running: true });
    } catch (err) {
      this._addMsg('error', '启动失败：' + (err.message || err));
    }
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

  // ---------- 文字 ----------
  onInput(e) { this.setData({ textIn: e.detail.value }); },

  async sendText() {
    const text = this.data.textIn.trim();
    if (!text) return;
    this.setData({ textIn: '' });
    this._addMsg('user', text);   // 文字轮不经 ASR，气泡在发送侧渲染（与 web 端一致）
    try {
      if (!this._ws) await this._connect();
      this._sendJson({ type: 'text', text });
    } catch (err) {
      this._addMsg('error', err.message || String(err));
    }
  },

  // ---------- 设置 ----------
  openSetup() { this.setData({ showSetup: true }); },
  onServerInput(e) { this.setData({ server: e.detail.value }); },
  onTokenInput(e) { this.setData({ token: e.detail.value }); },
  saveSetup() {
    const app = getApp();
    app.globalData.server = this.data.server.trim();
    app.globalData.token = this.data.token.trim();
    wx.setStorageSync('hl_server', app.globalData.server);
    wx.setStorageSync('hl_token', app.globalData.token);
    if (this._ws) { try { this._ws.close(); } catch (_) {} this._ws = null; }
    this.setData({ showSetup: false });
  }
});
