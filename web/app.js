// Hermes-live 前端主逻辑（结构参考 gpt-live public/app.js：els / setStatus / transcript）。
import { AudioIO } from '/web/audio.js';

const els = {
  status: document.getElementById('status'),
  orb: document.getElementById('orb'),
  stateLabel: document.getElementById('stateLabel'),
  transcript: document.getElementById('transcript'),
  btnToggle: document.getElementById('btnToggle'),
  btnInterrupt: document.getElementById('btnInterrupt'),
  btnNewSession: document.getElementById('btnNewSession'),
  textIn: document.getElementById('textIn'),
  btnSend: document.getElementById('btnSend'),
  selVoice: document.getElementById('selVoice'),
  selRate: document.getElementById('selRate'),
  btnAttach: document.getElementById('btnAttach'),
  fileIn: document.getElementById('fileIn'),
  dropOverlay: document.getElementById('dropOverlay'),
};

const STATE_LABEL = {
  idle: '已暂停',
  listening: '在听，你说',
  thinking: 'Hermes 思考中…',
  speaking: '播放回答（开口可打断）',
};

let ws = null;
let curTurn = -1;
let agentBubble = null;   // 当前 turn 的助手气泡
let agentTurn = -1;

const audio = new AudioIO({
  onFrame: (buf) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      const frame = new Uint8Array(1 + buf.byteLength);
      frame[0] = 0x01;
      frame.set(new Uint8Array(buf), 1);
      ws.send(frame);
    }
  },
  onLevel: (level) => {
    if (els.orb.className === 'listening') {
      const scale = 1 + Math.min(0.25, level * 2.2);
      els.orb.style.transform = `scale(${scale.toFixed(3)})`;
    } else {
      els.orb.style.transform = '';
    }
  },
  onPlaybackDone: (turn) => sendJson({ type: 'playback_done', turn }),
});

function sendJson(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

function setStatus(text) { els.status.textContent = text; }

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
  if (turn !== undefined && turn !== curTurn) {
    curTurn = turn;
    audio.setTurn(turn);   // 清播放缓冲：旧 turn 音频作废
  }
}

function handleMessage(msg) {
  switch (msg.type) {
    case 'hello':
      setStatus(`音色 ${msg.voice} · ASR ${msg.asr_model}` + (msg.note ? ` · ${msg.note}` : ''));
      break;
    case 'state':
      setState(msg.state, msg.turn);
      break;
    case 'asr_final':
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
    case 'error':
      addLine('error', msg.message);
      break;
  }
}

// 手机/远程模式：页面可由别处托管（如 iOS 壳的回环服务器），用 ?server=host:port&token=xxx 指回 Mac
const PAGE_PARAMS = new URLSearchParams(location.search);
const SERVER = PAGE_PARAMS.get('server') || location.host;
const TOKEN = PAGE_PARAMS.get('token') || '';

function connect() {
  return new Promise((resolve, reject) => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${SERVER}/ws${TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : ''}`);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => { setStatus('已连接'); resolve(); };
    ws.onerror = (e) => reject(e);
    ws.onclose = () => {
      setStatus('连接断开');
      setState('idle');
      els.btnToggle.textContent = '开始对话';
    };
    ws.onmessage = (e) => {
      if (typeof e.data === 'string') {
        handleMessage(JSON.parse(e.data));
      } else {
        const view = new Uint8Array(e.data);
        if (view[0] === 0x01) {
          const turn = view[1];
          audio.playAudio(turn, e.data.slice(2));
        }
      }
    };
  });
}

async function toggle() {
  if (audio.running) {
    sendJson({ type: 'stop' });
    await audio.stop();
    els.btnToggle.textContent = '开始对话';
    setState('idle');
    return;
  }
  els.btnToggle.disabled = true;
  try {
    if (!ws || ws.readyState !== WebSocket.OPEN) await connect();
    await audio.start();          // 用户手势内：授权麦克风 + resume AudioContext
    sendJson({ type: 'start' });
    els.btnToggle.textContent = '停止';
  } catch (err) {
    addLine('error', `启动失败：${err.message || err}`);
  } finally {
    els.btnToggle.disabled = false;
  }
}

els.btnToggle.addEventListener('click', toggle);
els.btnInterrupt.addEventListener('click', () => sendJson({ type: 'interrupt' }));
els.btnNewSession.addEventListener('click', () => {
  sendJson({ type: 'new_session' });
  els.transcript.innerHTML = '';
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
    await audio.start();
    sendJson({ type: 'start' });
    els.btnToggle.textContent = '停止';
  } catch { /* 无授权或自动播放受限：保持手动模式 */ }
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
