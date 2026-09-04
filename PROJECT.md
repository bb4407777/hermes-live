# PROJECT.md

## 项目概览

**Hermes-live**：本机 Hermes gateway 语音对话界面，参照 gpt-live 形态，语音层全免费自建。

- **定位**：律师事务所内部工具，便于语音快速查询案件、讨论法律问题
- **版本**：0.4.4（2026-08-10）
- **仓库**：`ssh://git@ssh.github.com:443/bb4407777/hermes-live.git`
- **部署**：launchd 常驻（`com.gaochengbin.hermes-live`，KeepAlive，高律师 2026-08-11 定：
  doubao 云端模式内存占用小，非常驻则 Mac 重启后隧道活着、8698 死，手机 502）

## 技术架构

### 整体流程

```
客户端（浏览器/小程序）
    ↓ WebSocket (ws://127.0.0.1:8698)
服务端 (server/main.py)
    ├─ silero VAD 分段 (server/vad.py)
    ├─ ASR 转写 (server/asr.py + server/qwen_asr.py)
    │   ├─ qwen3-asr sidecar（本地批量，复用 weSaw venv/权重，优先，无实时字幕）
    │   ├─ doubaoime-asr（云端流式，次选）
    │   ├─ sherpa-onnx（本地流式，备胎）
    │   └─ pywhispercpp（本地批处理，兜底）
    ├─ Hermes gateway 对话 (8647 /v1/chat/completions)
    ├─ 中文分句 (server/sentencer.py)
    └─ edge-tts 合成 (server/tts.py)
    ↓ 音频帧流回客户端
```

### 核心模块

| 模块 | 路径 | 职责 |
|------|------|------|
| 主服务 | `server/main.py` | WebSocket 服务、状态机、协议处理 |
| VAD | `server/vad.py` | silero 语音活动检测、分段 |
| ASR | `server/asr.py` | 多档语音识别（qwen/doubao/sherpa/whisper） |
| qwen sidecar | `server/qwen_asr.py` | Qwen3-ASR 本地 sidecar 客户端（复用 weSaw venv/权重，MPS 批量转写） |
| TTS | `server/tts.py` | edge-tts 合成、备胎接口 |
| 分句器 | `server/sentencer.py` | 中文分句、首句加速、markdown 清洗 |
| 配置 | `server/config.py` | 集中配置、环境变量覆盖 |
| 网页端 | `web/` | AudioWorklet、WebSocket 客户端 |
| 小程序端 | `miniprogram/` | RecorderManager、InnerAudioContext |

### 状态机

```
idle ─────────────> listening ─────────────> thinking ────────> speaking
  ↑                    │                         │                  │
  │                    │ VAD 检测到语音结束       │ LLM 开始返回     │
  │                    ↓                         ↓                  │
  └────────────────── ASR 识别完成 ────────────> 合成首句 ──────────┘
                                                                    │
                                                                    │ barge-in
                                                                    └──> idle
```

- **idle**：等待用户说话
- **listening**：录音中，VAD 监测语音段结束
- **thinking**：ASR 识别 + LLM 推理，忽略麦克风输入（防噪声误触发）
- **speaking**：TTS 播放，全双工，高门槛 VAD（≥0.85 持续 320ms）可打断

### WebSocket 协议

**客户端 → 服务端：**
- 音频二进制：`0x01` + PCM16（16kHz，单声道，16-bit LE）
- 控制消息（JSON）：
  - `{"type": "start"}`：开始对话
  - `{"type": "stop"}`：停止对话
  - `{"type": "text", "text": "..."}`：文字输入
  - `{"type": "new_session"}`：新会话
  - `{"type": "set_config", "voice": "...", "tts_rate": "..."}`：切换音色/语速

**服务端 → 客户端：**
- 音频二进制：`0x01` + turn 字节 + PCM16（24kHz）
  - turn 字节：当前轮次，打断后递增，客户端丢弃旧 turn 音频
- 事件消息（JSON）：
  - `{"type": "state", "state": "idle|listening|thinking|speaking"}`
  - `{"type": "asr_partial", "text": "..."}`：实时字幕（识别中间结果）
  - `{"type": "asr_final", "text": "..."}`：最终识别结果
  - `{"type": "agent_text", "text": "...", "done": false}`：LLM 流式输出
  - `{"type": "agent_text", "text": "", "done": true}`：LLM 完成
  - `{"type": "tts_sentence", "text": "..."}`：当前合成句
  - `{"type": "tts_end"}`：本轮 TTS 全部播放完成
  - `{"type": "error", "message": "..."}`：错误

## 客户端支持

### 1. 网页版（`web/`）

- **技术栈**：原生 JS + AudioWorklet + WebSocket
- **浏览器**：Safari 14.1+、Chrome 88+
- **特性**：全双工、实时字幕、音色/语速选择
- **启动**：服务端自动托管，访问 `http://127.0.0.1:8698`

### 2. 微信小程序（`miniprogram/`）

- **AppID**：`wxa5b11d3d8b80b07b`
- **技术栈**：原生小程序框架 + RecorderManager + WebSocket
- **特性**：
  - 实时字幕
  - 音色/语速选择
  - 新会话
  - **远程重启服务**（0.4.4 新增）
- **上传**：`node scripts/wx-upload.js [版本号] [描述]`
- **配置**：首次使用需设置服务器地址（`ws://IP:8698`）和 token

### 3. iOS App（`ios/`）

- **技术栈**：SwiftUI + AVAudioEngine 原生录音/播放（2026-08-14 起弃用 WKWebView 壳）
  - 采集：input tap → AVAudioConverter 16k PCM16，512 样本/帧（对齐 mic-processor.js）
  - 播放：AVAudioPlayerNode 24k PCM16，turn 过滤 + tts_end 排空后报 playback_done
  - 后台：`UIBackgroundModes=audio`，锁屏/切后台对话不断（WebView 壳做不到）
- **签名**：免费个人签名（7 天有效期）
- **工程**：`ios/HermesLive.xcodeproj`（`xcodegen generate` 由 `project.yml` 生成）

### 4. Tailscale 远程访问

- 见 `docs/mobile.md`
- 网页版零开发远程访问方案

## 配置

**配置文件**：`config.yaml`（复制 `config.yaml.example` 后修改）

**关键配置项**（完整列表见 `server/config.py`）：

```yaml
# 服务
host: 127.0.0.1        # 手机接入时改 0.0.0.0
port: 8698
auth_token: ""         # 非空时 /ws 必须带 ?token=（改 0.0.0.0 时务必设置）

# Hermes gateway
hermes_base_url: http://127.0.0.1:8647
hermes_model: k3       # 实际使用 pool-deepseek-v4-flash

# ASR
asr_backend: auto      # auto | doubao | sherpa | whispercpp | faster

# TTS
tts_voice: zh-CN-XiaoxiaoNeural
tts_rate: "+0%"

# VAD
vad_threshold: 0.5           # listening 档阈值
vad_end_silence_ms: 1100     # 尾静音判段结束（800 切碎思考停顿，2026-08-11 定 1100）
barge_threshold: 0.85        # speaking 档打断阈值
barge_hold_ms: 320           # 持续时长判打断
```

## 依赖管理

**Python 环境**：`py3.13` + venv（`.venv/`）

**ASR 三档（按优先级）：**

1. **doubaoime-asr**（云端，优先）
   - 包：`doubaoime-asr`（PyPI）
   - 凭据：`~/.config/doubao-asr/credentials.json`
   - 特点：实时流式，准确度高，需联网

2. **sherpa-onnx**（本地流式，备胎）
   - 包：`sherpa-onnx`
   - 模型：`/Users/gao/clone/expression-trainer/models/sherpa-onnx-streaming-paraformer-bilingual-zh-en`
   - 特点：RTF 0.03，边说边出字，与 expression-trainer 共用模型

3. **pywhispercpp**（本地批处理，兜底）
   - 包：`pywhispercpp`
   - 模型：`models/ggml/ggml-large-v3-turbo-q5_0.bin`（547MB，Metal GPU）
   - 特点：RTF 0.35，一字不差，首次启动自动下载

**TTS**：edge-tts（免费微软音色）

**VAD**：silero（pysilero-vad）

**安装注意**：
- pip 镜像：`-i https://mirrors.aliyun.com/pypi/simple/`（直连慢、清华 403）
- HF 权重：hf-mirror + `HF_HUB_DISABLE_XET=1`（Xet 绕镜像 401）

## 验证与测试

```bash
# 冒烟测试（健康检查 + edge-tts + 全链路）
scripts/smoke.sh

# 单元测试
.venv/bin/python -m pytest tests/ -q

# ASR 延迟基准
.venv/bin/python scripts/bench_asr.py

# WebSocket 协议回归（需服务已启动）
.venv/bin/python scripts/ws_regression.py
```

## 性能指标（M1 Pro 16GB，2026-08-08）

| 指标 | 结果 |
|------|------|
| **首包延迟** | 4.10s（ASR 1.0s + Hermes 3.2s + TTS 0.8s） |
| **ASR RTF** | whisper.cpp turbo q5+Metal **0.35**（一字不差）<br>sherpa-onnx paraformer **0.03**（实时流式） |
| **TTS 合成** | 快于实时（3.8s 音频 1.7s 合成完） |
| **内存占用** | whisper.cpp 常驻 ~0.7GB |
| **WS 回归** | 9/9 通过（全事件序、打断、turn 递增） |

**延迟大头**：Hermes 首 delta 3-4s（agent 大 prompt 预填充 + 池代理），会话变热后 3.9s→3.2s（前缀缓存）。

## 开发规范

### 代码风格

- Python：遵循 PEP 8，类型注解优先
- JavaScript：ES6+，async/await 优先
- 小程序：原生框架，避免引入第三方库

### 提交规范

- commit message：简洁中文描述，功能类别在前
- 示例：`小程序 + 服务端：增加重启服务功能`
- 每个功能点独立 commit，避免大杂烩

### 分支策略

- 主分支：`main`（直接开发，小项目无需 dev 分支）
- 标签：每次小程序上传打 tag（如 `v0.4.4`）

## 部署与运维

### 服务启动

**正式运行走 launchd 常驻**（2026-08-11 起）：

```bash
# 安装/更新（plist 模板在仓库，改动后重装）
cp scripts/launchd/com.gaochengbin.hermes-live.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.gaochengbin.hermes-live.plist

# 重启（自动识别 launchd，走 kickstart -k；网页/小程序重启按钮同此路径）
scripts/restart-service.sh

# 停用常驻
launchctl bootout gui/$(id -u)/com.gaochengbin.hermes-live
```

调试可前台跑（先 bootout，否则 launchd 已占 8698）：

```bash
.venv/bin/python -m server.main --port 8699   # 换端口最省事
```

### 小程序发布

```bash
# 上传代码
node scripts/wx-upload.js 0.4.4 "增加重启服务按钮"

# 登录微信公众平台设为体验版/提交审核
# https://mp.weixin.qq.com
```

### 远程重启（0.4.4+）

小程序内点击「重启」按钮，或手动调用：

```bash
curl -X POST http://127.0.0.1:8698/api/restart \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## 红线约束（对 Hermes gateway）

⚠️ **只读依赖，绝不修改**

- **绝不**重启/修改 gateway（launchd 常驻，PID 见 `pgrep -f hermes_cli.main`）
- **绝不** pip 安装/升级 hermes-agent（系统 site-packages 有本地补丁）
- **只读** state.db
- **禁碰** 8642 端口（qclaw_launcher 占用）
- **语音打断** = 断开 SSE 连接（gateway 自动 `agent.interrupt()`），不用更重手段

## 已知问题与待办

### 已知问题

1. ~~doubao ASR 连接偶尔失败/空结果~~（2026-08-11 已修：根因是 app 级单例被多连接共享互踩 +
   空闲 ~40s 被豆包远端掐流；现按连接实例化 + VAD start 懒建连 + 单活跃连接 + 前端断线自愈）
2. 首包延迟 4.1s，Hermes 首 delta 占 3.2s（agent prompt 大，网关池代理慢）
3. edge-tts 偶尔被风控（概率低，备胎 `say -v Tingting` 可用）
4. thinking/speaking 态半双工丢帧（Hermes 思考期说话被丢弃）——高律师 2026-08-11 定**不改**：
   排队下一轮的方案经常出 bug，维持半双工

### 待办事项

- [ ] 首包延迟优化（考虑 Hermes agent prompt 精简）
- [ ] TTS 备胎方案完善（Piper 本地合成）
- [ ] 小程序增加历史会话列表
- [ ] iOS App 推送通知支持

## 联系与支持

- **开发者**：高城斌律师
- **用途**：五邑律师事务所内部工具
- **协议**：内部项目，未开源
