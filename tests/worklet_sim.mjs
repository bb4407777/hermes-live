// 音频客户端的离线仿真：web 两个 worklet 用假 AudioWorkletProcessor 跑真实 process()，
// 小程序端 Player 用假 WebAudio 上下文 + 假时钟跑真实调度时序。
// 浏览器里没法跑 AudioWorklet，而这两段的坑全在时序/索引算术上（越界、NaN、收口时机），
// 只有把渲染量子按毫秒推进才看得见。
// 跑法：node tests/worklet_sim.mjs   （pytest 里由 test_worklet_sim.py 调）
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import assert from "node:assert/strict";

const SR = 48000;                       // 设备采样率
const QUANTUM = 128;

function load(file, envName, sampleRate = SR) {
  const messages = [];
  const sandbox = {
    sampleRate,
    registerProcessor: (_name, cls) => { sandbox.__cls = cls; },
    AudioWorkletProcessor: class {
      constructor() {
        this.port = { onmessage: null, postMessage: (m) => messages.push(m) };
      }
    },
    Math, Float32Array, Int16Array, ArrayBuffer, Number, console,
  };
  const code = readFileSync(process.env[envName]
    || fileURLToPath(new URL(`../web/worklets/${file}`, import.meta.url)), "utf8");
  vm.runInNewContext(code, sandbox);
  const node = new sandbox.__cls();
  return { node, messages };
}
const loadPlayer = (sampleRate = SR) => load("player-processor.js", "HL_PLAYER_WORKLET", sampleRate);
const loadMic = (sampleRate = SR) => load("mic-processor.js", "HL_MIC_WORKLET", sampleRate);

// 24k PCM16 → 下行 ArrayBuffer（前 2 字节协议头：0x01 + turn）
function frame(turn, ms, level = 0.4) {
  const n = Math.round(24 * ms);
  const buf = new ArrayBuffer(2 + n * 2);
  const view = new DataView(buf);
  view.setUint8(0, 1);
  view.setUint8(1, turn);
  for (let i = 0; i < n; i++) {
    const v = (i % 240 < 120 ? level : -level) * 32767;
    view.setInt16(2 + i * 2, v, true);
  }
  return buf;
}

const send = (node, buf, turn) => node.port.onmessage({
  data: { type: "audio", turn, pcm: buf, offset: 2 },
});
const pump = (node, quanta = 1) => {
  let loud = 0, bad = 0;
  for (let q = 0; q < quanta; q++) {
    // AudioWorklet 的 outputs 是「输出 → 声道数组 → 样本」，process 取 outputs[0][0]，
    // 所以这里要传 [[Float32Array]]，传错就会全程写进一个数字里、断言永远全静音也永远全 0
    const channels = [new Float32Array(QUANTUM)];
    node.process([], [channels]);
    for (const s of channels[0]) {
      if (!Number.isFinite(s)) bad++;
      else if (Math.abs(s) > 1e-4) loud++;
    }
  }
  return { loud, bad };
}

let failures = 0;
function check(name, fn) {
  try { fn(); console.log(`  ok   ${name}`); }
  catch (e) { failures++; console.log(`  FAIL ${name}\n       ${e.message}`); }
}

console.log("player-processor 仿真");

check("不到预灌水位不起播（首帧抖动不再掏空缓冲）", () => {
  const { node, messages } = loadPlayer();
  node.port.onmessage({ data: { type: "setTurn", turn: 1 } });
  send(node, frame(1, 60), 1);
  const r = pump(node, 10);
  assert.equal(r.loud, 0, "60ms < 120ms 水位却出声了");
  assert.equal(messages.length, 0, "还在攒缓冲就上报播完");
});

check("过水位后出声，且输出全为有限值", () => {
  const { node } = loadPlayer();
  node.port.onmessage({ data: { type: "setTurn", turn: 1 } });
  send(node, frame(1, 130), 1);
  const r = pump(node, 5);
  assert.ok(r.loud > 0, "过水位后仍无声");
  assert.equal(r.bad, 0, "输出出现 NaN/Inf");
});

check("没收到 end 时，句间 530ms 空隙不算播完", () => {
  const { node, messages } = loadPlayer();
  node.port.onmessage({ data: { type: "setTurn", turn: 1 } });
  send(node, frame(1, 200), 1);
  pump(node, 80);                       // 播完 200ms
  pump(node, 200);                      // 空转 ≈533ms
  assert.equal(messages.length, 0, "无 end 标记却上报播完（旧实现正是这样吃到回声）");
});

check("end + 尾巴播完 → 300ms 内上报 playback_done", () => {
  const { node, messages } = loadPlayer();
  node.port.onmessage({ data: { type: "setTurn", turn: 3 } });
  send(node, frame(3, 200), 3);
  pump(node, 60);                       // 播掉 ~160ms，剩 40ms 尾巴
  node.port.onmessage({ data: { type: "end", turn: 3 } });
  pump(node, 20);                       // 尾巴播完，进入欠载
  assert.equal(messages.length, 0, "提前上报");
  pump(node, 200);
  assert.equal(messages.length, 1, "收尾后 533ms 仍没上报（预灌门把欠载计数冻住了）");
  assert.equal(messages[0].turn, 3);
  assert.equal(messages[0].type, "done");
});

check("end 之后再来音频可继续上报（notifiedDone 复位）", () => {
  const { node, messages } = loadPlayer();
  node.port.onmessage({ data: { type: "setTurn", turn: 1 } });
  send(node, frame(1, 200), 1);
  node.port.onmessage({ data: { type: "end", turn: 1 } });
  pump(node, 400);
  assert.equal(messages.length, 1);
  send(node, frame(1, 300), 1);         // 工具间隙插缓冲语音那条路径
  pump(node, 200);
  pump(node, 400);
  assert.equal(messages.length, 2, "第二段音频没再收口");
});

check("旧 turn 音频被丢弃（打断后在途帧不响）", () => {
  const { node } = loadPlayer();
  node.port.onmessage({ data: { type: "setTurn", turn: 5 } });
  send(node, frame(4, 200), 4);
  const r = pump(node, 50);
  assert.equal(r.loud, 0, "旧 turn 的音频播出来了");
});

check("第 256 轮之后仍出声（帧头 turn 是 u8，JSON turn 是整数）", () => {
  const { node, messages } = loadPlayer();
  node.port.onmessage({ data: { type: "setTurn", turn: 259 } });   // 主线程给的是完整 turn
  send(node, frame(3, 200), 3);                                    // 音频头里是 259 & 0xFF
  const r = pump(node, 40);
  assert.ok(r.loud > 0, "turn 取模没对齐，音频全被当旧 turn 丢了");
  node.port.onmessage({ data: { type: "end", turn: 259 } });
  pump(node, 400);
  assert.equal(messages.length, 1, "done 没上报");
  assert.equal(messages[0].turn, 3, "done 里回的是字节 turn（app 侧要按 curTurn 换算）");
});

check("尾块比步长短也不 NaN（低采样率设备 ratio>1）", () => {
  const { node } = loadPlayer(16000);         // ratio = 1.5
  node.port.onmessage({ data: { type: "setTurn", turn: 1 } });
  send(node, frame(1, 130), 1);
  // 手工塞一个 1 样本的尾块
  const tiny = new ArrayBuffer(2 + 2);
  new DataView(tiny).setUint8(1, 1);
  node.port.onmessage({ data: { type: "audio", turn: 1, pcm: tiny, offset: 2 } });
  const r = pump(node, 300);
  assert.equal(r.bad, 0, "跨块边界出现 NaN");
});

check("主线程没给 offset（旧版 app.js 预切缓冲）也得出声", () => {
  // 静态资源不带 Cache-Control 时 Safari 各存各的新鲜度：缓存里的 0.4.6 app.js
  // 会把切掉 2 字节头的独立缓冲发给新 worklet。旧写法 (byteLength - undefined)>>1
  // = NaN>>1 = 0 → 每块都解成空块 → 全程静音，而麦克风/ASR 全正常，最难查。
  const { node } = loadPlayer();
  node.port.onmessage({ data: { type: "setTurn", turn: 1 } });
  const full = frame(1, 200);
  node.port.onmessage({ data: { type: "audio", turn: 1, pcm: full.slice(2) } });   // 无 offset
  const r = pump(node, 50);
  assert.ok(r.loud > 0, "无 offset 的下行块被解成空块 → 静音");
});

check("同一 turn 重复 setTurn 不啃掉已排队的音频", () => {
  const { node } = loadPlayer();
  node.port.onmessage({ data: { type: "setTurn", turn: 1 } });
  send(node, frame(1, 200), 1);
  node.port.onmessage({ data: { type: "setTurn", turn: 1 } });   // 主线程重发同 turn
  // 首块 200ms 必须还在：无脑清缓冲的旧写法在这里就直接静音（欠载 → loud=0）
  assert.ok(pump(node, 50).loud > 0, "重复 setTurn 把已排队的音频抹掉了");
  send(node, frame(1, 500), 1);
  let loud = pump(node, 50).loud;
  for (let q = 0; q < 250; q++) loud += pump(node, 1).loud;      // 再给 667ms 窗口
  assert.ok(loud > 20000, `整轮音频被啃成碎片（有声样本仅 ${loud}）`);
});

check("clear 丢缓冲但保住 turn：之后同 turn 音频照常播", () => {
  const { node, messages } = loadPlayer();
  node.port.onmessage({ data: { type: "setTurn", turn: 1 } });
  send(node, frame(1, 500), 1);
  node.port.onmessage({ data: { type: "clear" } });
  assert.equal(pump(node, 30).loud, 0, "clear 之后旧缓冲还在响");
  send(node, frame(1, 200), 1);
  assert.ok(pump(node, 40).loud > 0, "clear 把 turn 也弄坏了，同轮新音频播不出来");
  assert.equal(messages.filter((m) => m && m.type === "done").length, 0, "clear 后误报播完");
});

console.log("mic-processor 仿真");

function runMic(sr, seconds, gen) {
  const { node, messages } = loadMic(sr);
  const total = Math.round(sr * seconds);
  let thrown = null;
  try {
    for (let i = 0; i < total; i += QUANTUM) {
      const ch = new Float32Array(Math.min(QUANTUM, total - i));
      for (let j = 0; j < ch.length; j++) ch[j] = gen((i + j) / sr);
      node.process([[ch]]);                 // inputs[0][0] 才是声道数组
    }
  } catch (e) { thrown = e; }
  return {
    node, thrown,
    frames: messages.filter((m) => m instanceof ArrayBuffer),
    levels: messages.filter((m) => m && typeof m === "object" && "level" in m),
  };
}
const samples = (frames) => {
  const all = [];
  for (const b of frames) all.push(...new Int16Array(b));
  return all;
};

check("连续跑 0.5s 不抛（48k 首个量子就踩负偏移 = 麦克风直接哑）", () => {
  for (const sr of [48000, 44100, 16000]) {
    const r = runMic(sr, 0.5, (t) => 0.5 * Math.sin(2 * Math.PI * 440 * t));
    assert.equal(r.thrown, null, `@${sr}Hz process 抛了 ${r.thrown}`);
  }
});

check("上行帧恒 1024 字节，且帧数与时长吻合", () => {
  const r = runMic(48000, 1, (t) => 0.5 * Math.sin(2 * Math.PI * 440 * t));
  assert.ok(r.frames.length >= 31 && r.frames.length <= 32,
    `1s 应有 16000/512=31.25 帧，实得 ${r.frames.length}`);
  for (const b of r.frames) assert.equal(b.byteLength, 1024, "帧长不是 512 样本");
});

check("重采样倍率正确（440Hz 一秒钟 880 次过零）", () => {
  const s = samples(runMic(48000, 1, (t) => 0.5 * Math.sin(2 * Math.PI * 440 * t)).frames);
  assert.ok(s.length > 3000, `1s 该有约 16000 个上行样本，实得 ${s.length}`);
  let cross = 0;
  for (let i = 1; i < s.length; i++) if ((s[i - 1] < 0) !== (s[i] < 0)) cross++;
  const expect = 880 * (s.length / 16000);
  assert.ok(Math.abs(cross - expect) <= 6, `过零 ${cross} 偏离 ${expect.toFixed(0)} 太多（丢样/重样）`);
});

check("静音出 0、恒定 0.5 出 16384（不错位、不重复采样、不 NaN）", () => {
  const zeros = samples(runMic(48000, 0.2, () => 0).frames);
  assert.ok(zeros.length > 0 && zeros.every((v) => v === 0), "静音段出现了非 0");
  const dc = samples(runMic(48000, 0.2, () => 0.5).frames);
  assert.ok(dc.every((v) => v >= 16383 && v <= 16385), `DC 0.5 应≈16384，实得 ${dc[0]}/${dc[dc.length - 1]}`);
});

check("削波保护：超出 [-1,1) 的输入被夹进 int16 量程", () => {
  const s = samples(runMic(48000, 0.2, () => 3).frames);
  assert.ok(s.every((v) => v <= 32767 && v >= -32768), "溢出成了回绕值");
  assert.ok(s[0] > 30000, "该顶到满量程却没顶到");
});

check("电平消息按 ~16 量子节流（供状态球，不刷屏）", () => {
  const r = runMic(48000, 1, () => 0.1);
  assert.ok(r.levels.length >= 20 && r.levels.length <= 26, `1s 应有 ~23 条 level，实得 ${r.levels.length}`);
  assert.ok(r.levels.every((m) => Number.isFinite(m.level) && m.level >= 0));
});

// 小程序端 Player（miniprogram/utils/audio.js）：与 web worklet 同职责的第二套实现，
// 同样踩过"重复 setTurn 把正在播的音频清空"这一类时序坑，所以这里也用假 WebAudio
// 上下文 + 假时钟把调度时序跑一遍——微信开发者工具里做不到可重复的时序断言。
function loadMP() {
  let clock = 0, nextTimer = 1;
  const timers = new Map();          // id -> {at(秒), fn}
  const live = new Set();            // 已排度、尚未被 stop 的音源
  const ctx = {
    get currentTime() { return clock; },
    createBuffer(_ch, len, rate) {
      const d = new Float32Array(len);
      return { length: len, duration: len / rate, getChannelData: () => d };
    },
    createBufferSource() {
      const src = {
        buffer: null,
        connect() {},
        start(at) { src._at = at; live.add(src); },
        stop() { live.delete(src); },
      };
      return src;
    },
    close() {},
  };
  const code = readFileSync(
    process.env.HL_MP_AUDIO || fileURLToPath(new URL("../miniprogram/utils/audio.js", import.meta.url)),
    "utf8");
  const sandbox = {
    wx: { createWebAudioContext: () => ctx },
    Math, Float32Array, Int16Array, ArrayBuffer, Number, console,
    setTimeout: (fn, ms) => { const id = nextTimer++; timers.set(id, { at: clock + ms / 1000, fn }); return id; },
    clearTimeout: (id) => { timers.delete(id); },
    module: { exports: {} },
  };
  vm.runInNewContext(code, sandbox);
  const done = [];
  const player = new sandbox.module.exports.Player((t) => done.push(t));
  const advance = (sec) => {
    const target = clock + sec;
    for (;;) {
      let due = null;
      for (const [id, t] of timers) {
        if (t.at <= target && (due === null || t.at < due[1].at)) due = [id, t];
      }
      if (due === null) break;
      clock = due[1].at;
      timers.delete(due[0]);
      due[1].fn();
    }
    clock = target;
  };
  // 剩余未播的音频秒数：音源被 stop 掉就从这个数里消失，正是"尾音被啃"的可观测面
  const pending = () => [...live].reduce((s, src) =>
    s + Math.max(0, src._at + src.buffer.duration - clock), 0);
  const buf = (ms) => {
    const n = Math.round(24 * ms);
    const i16 = new Int16Array(n);
    i16.fill(1000);
    return i16.buffer;
  };
  return { player, done, advance, pending, buf };
}

console.log("小程序 Player 仿真");

check("tts_end + 排度播完 → 上报 playback_done", () => {
  const { player, done, advance, buf } = loadMP();
  player.setTurn(1);
  player.play(1, buf(400));
  player.endTurn(1);
  advance(0.2);
  assert.deepEqual(done, [], "还在播就该等");
  advance(2);
  assert.deepEqual(done, [1], `播完应上报一次，实得 ${JSON.stringify(done)}`);
});

check("同一 turn 重复 setTurn 不掐掉正在播的音频（state 每轮发三条）", () => {
  const { player, done, advance, pending, buf } = loadMP();
  player.setTurn(1);
  player.play(1, buf(400));
  player.play(1, buf(400));
  player.endTurn(1);
  const before = pending();
  assert.ok(before > 0.7, `夹具本身该有 ~0.8s 排度音频，实得 ${before}`);
  player.setTurn(1);              // 服务端超时/异常路径上的 state=listening(同一 turn)
  assert.ok(Math.abs(pending() - before) < 1e-6,
    `重复 setTurn 后剩余音频从 ${before}s 变成 ${pending()}s —— 尾音被掐了`);
  advance(3);
  assert.deepEqual(done, [1], "被清过 _ended/_doneTimer 就永远不会上报 playback_done");
});

check("换 turn 才丢缓冲：打断后旧音频不再上报", () => {
  const { player, done, advance, pending, buf } = loadMP();
  player.setTurn(1);
  player.play(1, buf(400));
  player.endTurn(1);
  player.setTurn(2);              // 打断：turn 递增
  assert.equal(pending(), 0, "旧 turn 音频必须立刻停掉");
  advance(3);
  assert.deepEqual(done, [], "被打断那一轮不该再上报播完");
});

process.exit(failures ? 1 : 0);
