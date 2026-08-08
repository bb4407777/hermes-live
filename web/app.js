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

function connect() {
  return new Promise((resolve, reject) => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws`);
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
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    connect().then(() => sendJson({ type: 'text', text }));
  } else {
    sendJson({ type: 'text', text });
  }
}
els.btnSend.addEventListener('click', sendText);
els.textIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') sendText(); });

setState('idle');
