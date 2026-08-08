#!/usr/bin/env node
/* hermes-live 启动器：npx hermes-live 一行跑起本地语音对话服务。
 * 职责：找 python3 → ~/.hermes-live 下建 venv 装依赖 → 下 whisper 权重 → 生成配置 → 起服务 → 开浏览器。
 * 只用 Node 内置模块；下载用系统 curl（macOS/Linux/Win10+ 均自带）。 */
'use strict';
const { spawnSync, spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');

const PKG_ROOT = __dirname;
const DATA = path.join(os.homedir(), '.hermes-live');
const IS_WIN = process.platform === 'win32';
const VENV = path.join(DATA, 'venv');
const VENV_BIN = path.join(VENV, IS_WIN ? 'Scripts' : 'bin');
const PY = path.join(VENV_BIN, IS_WIN ? 'python.exe' : 'python');
const CONFIG = path.join(DATA, 'config.yaml');
const GGML = path.join(DATA, 'models', 'ggml', 'ggml-large-v3-turbo-q5_0.bin');

// ---------- 参数 ----------
const args = process.argv.slice(2);
function opt(name, dflt) {
  const i = args.indexOf('--' + name);
  return i >= 0 && args[i + 1] && !args[i + 1].startsWith('--') ? args[i + 1] : dflt;
}
const FLAGS = {
  port: opt('port', '8698'),
  host: opt('host', '127.0.0.1'),
  apiUrl: opt('api-url', 'http://127.0.0.1:8647'),
  apiKey: opt('api-key', 'hermes-local-key'),
  model: opt('model', 'k3'),
  token: opt('token', ''),
  mirror: args.includes('--mirror'),
  skipModel: args.includes('--skip-model'),
  noBrowser: args.includes('--no-browser'),
};
if (args.includes('--help') || args.includes('-h')) {
  console.log(`hermes-live —— 和任意 OpenAI 兼容端点实时语音对话（本地 VAD+whisper+edge-tts）

用法: npx hermes-live [选项]
  --api-url <url>   OpenAI 兼容端点 (默认 http://127.0.0.1:8647)
  --api-key <key>   端点密钥        (默认 hermes-local-key)
  --model <name>    模型名          (默认 k3)
  --port <n>        服务端口        (默认 8698)
  --host <ip>       监听地址        (默认 127.0.0.1；0.0.0.0 须配 --token)
  --token <s>       外部接入鉴权 token
  --mirror          中国大陆网络：pip 走阿里云、模型走 hf-mirror
  --skip-model      跳过 whisper.cpp 权重下载（用 faster-whisper 兜底）
  --no-browser      就绪后不自动开浏览器

数据目录 ~/.hermes-live/（venv、模型、config.yaml——改配置编辑它即可）`);
  process.exit(0);
}

function die(msg) { console.error('\n✗ ' + msg); process.exit(1); }
function step(msg) { console.log('\n▸ ' + msg); }

// ---------- 1. python3 ----------
function findPython() {
  for (const c of ['python3', 'python']) {
    const r = spawnSync(c, ['-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'], { stdio: 'ignore' });
    if (r.status === 0) return c;
  }
  die('需要 Python ≥3.10（brew install python3 / apt install python3 / python.org）');
}

// ---------- 2. venv + 依赖 ----------
function ensureVenv(python) {
  fs.mkdirSync(DATA, { recursive: true });
  if (!fs.existsSync(PY)) {
    step('创建 Python 虚拟环境 ' + VENV);
    if (spawnSync(python, ['-m', 'venv', VENV], { stdio: 'inherit' }).status !== 0) die('venv 创建失败');
  }
  const marker = path.join(DATA, '.deps-ok');
  const req = path.join(PKG_ROOT, 'requirements-core.txt');
  if (fs.existsSync(marker) && fs.statSync(marker).mtimeMs >= fs.statSync(req).mtimeMs) return;
  step('安装依赖（首次约 2-5 分钟）…');
  const pipArgs = ['-m', 'pip', 'install', '-r', req];
  if (FLAGS.mirror) pipArgs.push('-i', 'https://mirrors.aliyun.com/pypi/simple/');
  if (spawnSync(PY, pipArgs, { stdio: 'inherit' }).status !== 0)
    die('依赖安装失败。国内网络加 --mirror 重试；pywhispercpp 编译失败可先装 cmake，或加 --skip-model 用兜底后端');
  fs.writeFileSync(marker, new Date().toISOString());
}

// ---------- 3. whisper 权重 ----------
function ensureModel() {
  if (FLAGS.skipModel || fs.existsSync(GGML)) return;
  const host = FLAGS.mirror ? 'hf-mirror.com' : 'huggingface.co';
  const url = `https://${host}/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin`;
  step('下载 whisper 权重（574MB，仅首次）…');
  fs.mkdirSync(path.dirname(GGML), { recursive: true });
  const tmp = GGML + '.part';
  const r = spawnSync('curl', ['-L', '--fail', '-C', '-', '-o', tmp, url], { stdio: 'inherit' });
  // -C - 续传：文件其实已完整时服务端可能回 416 让 curl 报错，以落盘大小为准
  if (!fs.existsSync(tmp) || fs.statSync(tmp).size < 500e6) {
    die('权重下载失败（已下部分保留，重跑续传）。国内网络加 --mirror；或加 --skip-model 跳过（faster-whisper 兜底会自动下载）');
  }
  if (r.status !== 0) console.log('  （curl 返回非零但文件大小达标，按已完成处理）');
  fs.renameSync(tmp, GGML);
}

// ---------- 4. 配置 ----------
function ensureConfig() {
  if (fs.existsSync(CONFIG)) { console.log('  使用已有配置 ' + CONFIG); return; }
  step('生成配置 ' + CONFIG);
  fs.writeFileSync(CONFIG, `# hermes-live 配置（npx hermes-live 生成；全部可改，键见包内 server/config.py）
host: ${FLAGS.host}
port: ${FLAGS.port}
auth_token: '${FLAGS.token}'
hermes_base_url: ${FLAGS.apiUrl}
hermes_api_key: ${FLAGS.apiKey}
hermes_model: ${FLAGS.model}
asr_download_root: ${JSON.stringify(path.join(DATA, 'models'))}
asr_ggml_model: ${JSON.stringify(GGML)}
`);
}

// ---------- 5. 起服务 + 开浏览器 ----------
function waitHealthy(url, timeoutMs, cb) {
  const t0 = Date.now();
  (function poll() {
    http.get(url, (res) => {
      res.resume();
      if (res.statusCode === 200) return cb(true);
      retry();
    }).on('error', retry);
    function retry() {
      if (Date.now() - t0 > timeoutMs) return cb(false);
      setTimeout(poll, 1000);
    }
  })();
}

function openBrowser(url) {
  const cmd = process.platform === 'darwin' ? ['open', url]
    : IS_WIN ? ['cmd', '/c', 'start', '', url] : ['xdg-open', url];
  try { spawn(cmd[0], cmd.slice(1), { stdio: 'ignore', detached: true }).unref(); } catch (_) {}
}

function main() {
  const python = findPython();
  ensureVenv(python);
  ensureModel();
  ensureConfig();
  step(`启动服务（端口 ${FLAGS.port}，模型加载首包约 10s）…`);
  const child = spawn(PY, ['-m', 'server.main', '--config', CONFIG], { cwd: PKG_ROOT, stdio: 'inherit' });
  child.on('exit', (code) => process.exit(code ?? 0));
  for (const sig of ['SIGINT', 'SIGTERM']) process.on(sig, () => child.kill(sig));
  const page = `http://127.0.0.1:${FLAGS.port}`;
  waitHealthy(page + '/api/health', 15 * 60 * 1000, (ok) => {
    if (!ok) return console.error('等服务就绪超时——看上方日志排查（LLM 端点连不上不影响页面打开）');
    console.log(`\n✓ 就绪：${page}（Ctrl+C 停止）`);
    if (!FLAGS.noBrowser) openBrowser(page);
  });
}

main();
