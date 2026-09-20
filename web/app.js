// Hermes-live 前端主逻辑（结构参考 gpt-live public/app.js：els / setStatus / transcript）。
import { AudioIO } from '/web/audio.js?v=0.4.8';  // 版本号=缓存破壁，改版同步 audio.js 的 WORKLET_V

const els = {
  status: document.getElementById('status'),
  orb: document.getElementById('orb'),
  stateLabel: document.getElementById('stateLabel'),
  transcript: document.getElementById('transcript'),
  btnToggle: document.getElementById('btnToggle'),
  btnInterrupt: document.getElementById('btnInterrupt'),
  btnNewSession: document.getElementById('btnNewSession'),
  btnRestart: document.getElementById('btnRestart'),
  textIn: document.getElementById('textIn'),
  btnSend: document.getElementById('btnSend'),
  selVoice: document.getElementById('selVoice'),
  selRate: document.getElementById('selRate'),
  btnAttach: document.getElementById('btnAttach'),
  fileIn: document.getElementById('fileIn'),
  dropOverlay: document.getElementById('dropOverlay'),
  partialText: document.getElementById('partialText'),
};

const STATE_LABEL = {
  idle: '已暂停',
  listening: '在听，你说',
  thinking: 'Hermes 思考中…',
  // 半双工（服务端 session.py 在 speaking 不处理上行帧）：开口打断不了，用「打断」按钮
  speaking: '播放回答中…（点「打断」可停）',
};

let ws = null;
let curTurn = -1;
let agentBubble = null;   // 当前 turn 的助手气泡
let agentTurn = -1;

const audio = new AudioIO({
  onFrame: (buf) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    // 上行背压：隧道抖动时帧队列无界增长会让 VAD 整体延后（体感"反应慢/断续"），
    // 宁可丢麦克风帧也不让旧音频压着新音频
    if (ws.bufferedAmount > 64 * 1024) return;
    const frame = new Uint8Array(1 + buf.byteLength);
    frame[0] = 0x01;
    frame.set(new Uint8Array(buf), 1);
    ws.send(frame);
  },
  onLevel: (level) => {
    if (els.orb.className === 'listening') {
      const scale = 1 + Math.min(0.25, level * 2.2);
      els.orb.style.transform = `scale(${scale.toFixed(3)})`;
    } else {
      els.orb.style.transform = '';
    }
  },
  onPlaybackDone: (turn) => {
    // worklet 回的是下行音频头那个字节（u8）；服务端按完整 turn 记 Event，
    // 所以用主线程的 curTurn 回报。字节对得上才认，避免旧 turn 的迟到 done
    // 把新一轮的等待窗提前戳开（那样喇叭还在播就开麦了）。
    if ((curTurn & 0xFF) === turn) sendJson({ type: 'playback_done', turn: curTurn });
  },
});

function sendJson(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

function setStatus(text) { els.status.textContent = text; }

// ── 2026-09-20 手机端无声诊断：屏上计数，区分「帧没到」还是「到了没播」 ──
let dbgRxFrames = 0, dbgRxBytes = 0, dbgLastReason = '';
function dbgText() {
  const a = (typeof audio !== 'undefined' && audio && audio.dbg) ? audio.dbg() : '';
  return `帧${dbgRxFrames}/${(dbgRxBytes / 1024).toFixed(0)}KB${a ? ' · ' + a : ''}`;
}
function dbgShow() { setStatus(dbgText()); }

function addBubble(cls, text) {
  const div = document.createElement('div');
  div.className = `bubble ${cls}`;
  div.textContent = text;
  els.transcript.appendChild(div);
  els.transcript.scrollTop = els.transcript.scrollHeight;
  return div;
}

function addLine(cls, text) {
  const div = document.createElement('div');
  div.className = cls;
  div.textContent = text;
  els.transcript.appendChild(div);
  els.transcript.scrollTop = els.transcript.scrollHeight;
  return div;
}

function setState(state, turn) {
  els.orb.className = state;
  els.stateLabel.textContent = STATE_LABEL[state] || state;
  els.btnInterrupt.disabled = !(state === 'thinking' || state === 'speaking');
  if (state !== 'listening') els.partialText.textContent = '';
  if (turn !== undefined && turn !== curTurn) {
    curTurn = turn;
    audio.setTurn(turn);   // 清播放缓冲：旧 turn 音频作废
  }
  // 半双工：speaking/thinking 时停发麦克风帧，listening 时恢复（与小程序行为对齐）
  audio.setMicPaused(state === 'speaking' || state === 'thinking');
}

function handleMessage(msg) {
  switch (msg.type) {
    case 'hello':
      setStatus(`音色 ${msg.voice} · ASR ${msg.asr_model}` + (msg.note ? ` · ${msg.note}` : ''));
      break;
    case 'state':
      setState(msg.state, msg.turn);
      break;
    case 'asr_partial':
      els.partialText.textContent = msg.text;
      break;
    case 'asr_final':
      els.partialText.textContent = '';
      addBubble('user', msg.text);
      break;
    case 'agent_delta':
      if (agentTurn !== msg.turn || !agentBubble) {
        agentBubble = addBubble('agent', '');
        agentTurn = msg.turn;
      }
      agentBubble.textContent += msg.text;
      els.transcript.scrollTop = els.transcript.scrollHeight;
      break;
    case 'agent_done':
      agentBubble = null;
      break;
    case 'tool_progress':
      addLine('tool', `${msg.emoji || '⚙️'} ${msg.label || msg.tool || '工具执行中'}${msg.status === 'completed' ? ' ✓' : '…'}`);
      break;
    case 'tts_sentence':
      break; // 预留：逐句高亮
    case 'tts_end':
      // 本 turn 音频已发完：worklet 只有在这个标记下才允许上报播完，
      // 否则句间 >300ms 的正常空隙会被当成播完，服务端提前回到 listening 吃到回声
      if (msg.turn === curTurn) audio.setTurnEnd(msg.turn);
      break;
    case 'replaced':
      // 服务端单活跃连接：本页被新页面顶替，主动退场，不再自动重连
      wantLive = false;
      clearReconnect();
      audio.stop();
      if (ws) ws.close();
      setStatus('已在其他页面打开，本页停止');
      setState('idle');
      els.btnToggle.textContent = '开始对话';
      break;
    case 'error':
      addLine('error', msg.message);
      break;
  }
}

// 手机/远程模式：页面可由别处托管（如 iOS 壳的回环服务器），用 ?server=host:port&token=xxx 指回 Mac
const PAGE_PARAMS = new URLSearchParams(location.search);
const SERVER = PAGE_PARAMS.get('server') || location.host;

// token 只从 URL 读、不落地的话，书签里少带一次 ?token= 就是整页不可用
// （实测 2026-09-19 手机 Safari 打开裸域名 → /ws 401，页面看着在收音其实一个字节没发出去）
const TOKEN = PAGE_PARAMS.get('token') || localStorage.getItem('hl_token') || '';
if (PAGE_PARAMS.get('token')) {
  try { localStorage.setItem('hl_token', PAGE_PARAMS.get('token')); } catch { /* 隐私模式写不进 */ }
}

// WS 被 401 拒掉时浏览器只给一个裸 Event，页面显示"启动失败"，用户猜不到是少 token。
// 探一次 /api/health（该端点不鉴权），确认真的开了鉴权再把话说清楚。
let authHintShown = false;
async function hintAuth() {
  if (authHintShown || TOKEN) return;
  authHintShown = true;
  try {
    const proto = location.protocol === 'https:' ? 'https' : 'http';
    const resp = await fetch(`${proto}://${SERVER}/api/health`);
    const data = await resp.json();
    if (data.auth_required) addLine('error', '服务要求 token：请在网址后加 ?token=xxx（打开一次即记住）');
  } catch { /* 服务确实没起或网络不通：保持原始报错 */ }
}

// ---- 连接管理：单飞防重入 + 断线自愈 ----
// wantLive = 用户意图“正在对话”。断线（息屏/切后台/隧道抖动）时麦克风还开着，
// 老逻辑只改状态文字不重连——用户看着在收音，实际连接已死。现在意图在就自动重连恢复。

let wantLive = false;
let connectPromise = null;   // 单飞：autostart 与手点竞态曾各建一条 WS，两个服务端 Session 抢 ASR
let reconnectTimer = null;
let reconnectDelay = 1000;

function connect() {
  if (ws && ws.readyState === WebSocket.OPEN) return Promise.resolve();
  if (connectPromise) return connectPromise;
  connectPromise = new Promise((resolve, reject) => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const socket = new WebSocket(`${proto}://${SERVER}/ws${TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : ''}`);
    ws = socket;
    socket.binaryType = 'arraybuffer';
    // 旧 socket 的回调可能在重连后才到（隧道抖动时尤其常见）。不判身份就会把新连接的
    // 状态改成 idle / 停掉刚建好的 audio，表现为"明明连上了却一直没声音"。
    const stale = () => socket !== ws;
    socket.onopen = () => { if (!stale()) { setStatus('已连接'); resolve(); } };
    socket.onerror = (e) => { if (!stale()) { hintAuth(); reject(e); } };
    socket.onclose = (e) => {
      if (stale()) return;
      if (e.code === 4001) {
        // 被新连接顶替（服务端单活跃连接）：本页退场，不与新页面互踢
        wantLive = false;
        clearReconnect();
        audio.stop();
        setStatus('已在其他页面打开，本页停止');
        setState('idle');
        els.btnToggle.textContent = '开始对话';
        return;
      }
      setState('idle');
      if (wantLive) {
        scheduleReconnect();
      } else {
        setStatus('连接断开');
        els.btnToggle.textContent = '开始对话';
      }
    };
    socket.onmessage = (e) => {
      if (stale()) return;
      if (typeof e.data === 'string') {
        handleMessage(JSON.parse(e.data));
      } else {
        const view = new Uint8Array(e.data);
        if (view[0] === 0x01) {
          const turn = view[1];
          dbgRxFrames++; dbgRxBytes += e.data.byteLength;
          if (dbgRxFrames === 1 || dbgRxFrames % 10 === 0) dbgShow();
          // 直接交出原 buffer + 头长偏移：128ms/句的音频不必每块再拷一次
          audio.playAudio(turn, e.data, 2);
        } else {
          dbgLastReason = '未知帧型 0x' + view[0].toString(16);
          dbgShow();
        }
      }
    };
  }).finally(() => { connectPromise = null; });
  return connectPromise;
}

function clearReconnect() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  reconnectDelay = 1000;
}

function scheduleReconnect() {
  if (reconnectTimer || !wantLive) return;
  setStatus(`连接断开，${Math.round(reconnectDelay / 1000)}s 后重连…`);
  reconnectTimer = setTimeout(async () => {
    reconnectTimer = null;
    if (!wantLive) return;
    try {
      await connect();
      reconnectDelay = 1000;
      // iOS 挂起后 AudioContext 可能已 close：光重连 WS 会出现"已重连但永远 listening"
      try {
        if (audio.running) audio.resumeIfNeeded();
        else await audio.start();
      } catch { /* 授权/自动播放受限：保持现有页面，用户手点按钮再恢复 */ }
      pushConfig();
      sendJson({ type: 'start' });   // 麦克风一直开着，恢复 listening 后帧自动续上
      setStatus('已重连');
    } catch {
      reconnectDelay = Math.min(reconnectDelay * 2, 10000);
      scheduleReconnect();
    }
  }, reconnectDelay);
}

// 手机息屏/切后台时 iOS 会挂起网络与 AudioContext，回前台立即检查补连（不等退避计时）。
// 只补 WS 不够：AudioContext 仍是 suspended，麦克风一帧不出、下行也不播，
// 界面看着 listening 其实是死的。resume 必须在回前台的手势回调里补一次。
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible' || !wantLive) return;
  audio.resumeIfNeeded();
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    clearReconnect();
    scheduleReconnect();
  }
});

async function toggle() {
  if (wantLive) {
    wantLive = false;
    clearReconnect();
    sendJson({ type: 'stop' });
    await audio.stop();
    els.btnToggle.textContent = '开始对话';
    setState('idle');
    return;
  }
  els.btnToggle.disabled = true;
  try {
    if (!ws || ws.readyState !== WebSocket.OPEN) await connect();
    if (!audio.running) await audio.start();  // 用户手势内：授权麦克风 + resume AudioContext
    pushConfig();                 // 连接后立即同步当前语速/音色到服务端
    sendJson({ type: 'start' });
    wantLive = true;
    els.btnToggle.textContent = '停止';
  } catch (err) {
    addLine('error', `启动失败：${err.message || err}`);
  } finally {
    els.btnToggle.disabled = false;
  }
}

els.btnToggle.addEventListener('click', toggle);
els.orb.addEventListener('click', toggle);
els.btnInterrupt.addEventListener('click', () => sendJson({ type: 'interrupt' }));
els.btnNewSession.addEventListener('click', () => {
  sendJson({ type: 'new_session' });
  // 服务端 new_session 会 bump turn，但按钮到 state 事件之间还有一小段窗口；
  // 本地先清播放队列，旧会话的尾巴才不会混进新会话的第一句
  audio.clear();
  els.transcript.innerHTML = '';
  agentBubble = null;
  agentTurn = -1;
});

els.btnRestart.addEventListener('click', async () => {
  if (!confirm('确定要重启服务吗？重启期间会短暂断开连接。')) return;
  els.btnRestart.disabled = true;
  els.btnRestart.textContent = '重启中...';
  try {
    const proto = location.protocol === 'https:' ? 'https' : 'http';
    const url = `${proto}://${SERVER}/api/restart${TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : ''}`;
    await fetch(url, { method: 'POST' });
    addLine('tool', '✓ 服务重启中，3秒后自动重连...');
    // 关闭当前连接
    if (ws) ws.close();
    ws = null;
    // 4秒后重连（给服务2秒退出+2秒启动的时间）
    setTimeout(() => {
      connect()
        .then(() => addLine('tool', '✓ 重连成功'))
        .catch(() => addLine('error', '重连失败，请手动刷新页面'));
    }, 4000);
  } catch (err) {
    addLine('error', `重启失败：${err.message || err}`);
  } finally {
    setTimeout(() => {
      els.btnRestart.disabled = false;
      els.btnRestart.textContent = '重启服务';
    }, 6000);
  }
});

function sendText() {
  const text = els.textIn.value.trim();
  if (!text) return;
  els.textIn.value = '';
  // 文字轮不经 ASR、服务端不会回 asr_final，用户气泡在发送侧渲染
  addBubble('user', text);
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    connect().then(() => sendJson({ type: 'text', text }));
  } else {
    sendJson({ type: 'text', text });
  }
}
els.btnSend.addEventListener('click', sendText);
els.textIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') sendText(); });

// 打开即聊：页面加载完自动开始对话（壳内麦克风由壳放行、TCC 授权过即静默；
// 浏览器里授权/自动播放受限则静默回退手动按钮）。?autostart=0 可关。
(async () => {
  if (PAGE_PARAMS.get('autostart') === '0') return;
  try {
    if (!ws || ws.readyState !== WebSocket.OPEN) await connect();
    if (!audio.running) await audio.start();
    sendJson({ type: 'start' });
    wantLive = true;
    els.btnToggle.textContent = '停止';
  } catch { /* 无授权或自动播放受限：保持手动模式（连接留用，手点时复用同一条） */ }
})();

function pushConfig() {
  sendJson({ type: 'set_config', voice: els.selVoice.value, tts_rate: els.selRate.value });
}
els.selVoice.addEventListener('change', pushConfig);
els.selRate.addEventListener('change', pushConfig);

// ---- 附件（架构照抄 areco：上传落盘 → 绝对路径当纯文本回填输入框，可继续编辑）----

function fillPaths(paths) {
  const insert = paths.join(' ');
  const cur = els.textIn.value;
  els.textIn.value = cur ? `${cur.replace(/\s+$/, '')} ${insert} ` : `${insert} `;
  els.textIn.focus();
  els.textIn.setSelectionRange(els.textIn.value.length, els.textIn.value.length);
}

async function uploadFiles(files) {
  if (!files.length) return;
  els.btnAttach.textContent = '⏳';
  els.btnAttach.disabled = true;
  const paths = [];
  try {
    for (const f of files) {
      const proto = location.protocol === 'https:' ? 'https' : 'http';
      const url = `${proto}://${SERVER}/api/files/upload?name=${encodeURIComponent(f.name)}` +
        (TOKEN ? `&token=${encodeURIComponent(TOKEN)}` : '');
      const resp = await fetch(url, { method: 'POST', body: f });
      if (!resp.ok) throw new Error(`${f.name}: HTTP ${resp.status}`);
      const data = await resp.json();
      paths.push(data.data.path);
    }
    fillPaths(paths);
  } catch (err) {
    addLine('error', `附件上传失败：${err.message || err}`);
  } finally {
    els.btnAttach.textContent = '📎';
    els.btnAttach.disabled = false;
    els.fileIn.value = '';
  }
}

els.btnAttach.addEventListener('click', () => els.fileIn.click());
els.fileIn.addEventListener('change', () => uploadFiles([...els.fileIn.files]));

// document 级拖拽 + 计数器消除子元素 enter/leave 抖动（areco useFileDrop 同款）
let dragCount = 0;
document.addEventListener('dragenter', (e) => {
  e.preventDefault();
  if (++dragCount === 1) els.dropOverlay.classList.add('show');
});
document.addEventListener('dragover', (e) => e.preventDefault());
document.addEventListener('dragleave', (e) => {
  e.preventDefault();
  if (--dragCount <= 0) { dragCount = 0; els.dropOverlay.classList.remove('show'); }
});
document.addEventListener('drop', (e) => {
  e.preventDefault();               // 必须同步调用，否则浏览器直接打开文件
  dragCount = 0;
  els.dropOverlay.classList.remove('show');
  const files = [...(e.dataTransfer?.files || [])];
  if (files.length) uploadFiles(files);
});

setState('idle');
