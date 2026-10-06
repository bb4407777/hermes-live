# PROJECT.md

## 项目概览

**Hermes-live**：本机 Hermes gateway 语音对话界面，参照 gpt-live 形态，语音层全免费自建。

- **定位**：律师事务所内部工具，便于语音快速查询案件、讨论法律问题
- **版本**：0.4.8（2026-09-20）——版本号单一来源 `server/__init__.py`，`/api/health` 回显
- **仓库**：`ssh://git@ssh.github.com:443/bb4407777/hermes-live.git`
- **部署**：launchd 常驻（`com.gaochengbin.hermes-live`，KeepAlive，高律师 2026-08-11 定：
  doubao 云端模式内存占用小，非常驻则 Mac 重启后隧道活着、8698 死，手机 502）
- **对话后端桥**（2026-10-05 起）：launchd 常驻 `com.gaochengbin.hermes-live-acp-bridge`
  （`server/acp_bridge.py`），监听 127.0.0.1:8647 顶替退役 Hermes gateway 的插槽。CLI 与
  模型跟 config.yaml `acp_cli`/`acp_model`（当前 AI 版 CLI + deepseek-v4.1-flash，
  2026-10-06 高律师定；沿革：deepseek-v4.1-flash 主版 → glm-5.3-flash 主版 → AI 版 DS）
- **后端沿革**：Hermes gateway（~2026-10-02 退役）→ 中枢通版 CodeBuddy 经 ACP 桥
  （2026-10-05 高律师定「转为中枢通版CodeBuddy的DeepSeek」，同日改「改glm5.3flash模型」）。
  hermes-live 本体零改动：每轮现发 POST、无缓存状态，桥在即自愈，主服务无需重启

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
    ├─ 对话后端（2026-10-05 起）：ACP 桥（server/acp_bridge.py，launchd 常驻，8647）
    │   └─ 中枢同款 CodeBuddy 主版 CLI（ACP stdio，deepseek-v4.1-flash + hindsight MCP）
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

### 会话身份（跨案隔离，2026-09-19 修复）

每轮请求都带 `X-Hermes-Session-Id`，值由本服务随机铸造（`hl-<uuid4[:16]>`）：

- **为什么必须自己铸造**：请求头缺这个字段时，网关改用
  `sha256(system_prompt + "\n" + 首条用户消息)` 派生会话身份，并从 `state.db` 把那条会话的
  历史整段捞回来。语音场景的首句几乎必然是"在吗""你好"这类固定开场白，实测一次
  `在吗` 就复活了 2026-09-04 某案的会话：第二轮 prompt 从 11801 token 涨到 42771 token，
  首 delta 从 4.7s 涨到 11.8s。**对律师工具这是跨案串内容，不是性能问题。**
- **生命周期**：`HermesClient` 是 app 级单例 → 同一个 session id 跨 WS 重连保持（页面刷新
  不丢上下文）；点「新会话」或重启服务才轮换。`new_session` 同时 `turn += 1`，否则客户端
  会继续播放上一轮的残留音频。
- **`/api/health` 不回显 session id**（该端点不鉴权，属外部可读信息）；只经 WS 的 `hello`
  发给已鉴权的连接。
- **2026-10-05 起语义（ACP 桥）**：hl- 会话 id 由桥映射为 CLI 的 ACP sessionId（首见即建，
  含启动预热的一个空会话免首轮冷启动）；同一 hl- 跨 WS 重连保持上下文，点「新会话」换新
  ACP 会话，桥/CLI 重启则映射清空、各 hl- 重新开新会话（上下文清零）。CLI 以
  `--no-session-persistence` 跑，语音会话不落盘。

### 状态机

```
idle ──start──> listening ──VAD 段尾/文字──> thinking ──首句音频──> speaking
  ↑                  ↑                          │                      │
  │                  └──── playback_done ───────┴────── 打断(按钮) ────┘
  └──stop                                            （turn+1，旧音频帧作废）
```

- **idle**：等待用户说话
- **listening**：录音中，VAD 监测语音段结束
- **thinking**：ASR 识别 + LLM 推理，忽略麦克风输入（防噪声误触发）
- **speaking**：TTS 播放。**半双工**（高律师 2026-08-11 定：排队下一轮的方案常出 bug，
  维持半双工）——前端在 thinking/speaking 停发麦克风帧，服务端对应分支是 `pass`，
  `BargeDetector` 因此实际不参与决策；唯一打断入口是客户端「打断」按钮（`{"type":"interrupt"}`）

### WebSocket 协议

**客户端 → 服务端：**
- 音频二进制：`0x01` + PCM16（16kHz，单声道，16-bit LE，固定 512 样本 = 1024 字节 = 32ms）
- 控制消息（JSON）：
  - `{"type": "start", "ptt": false}`：开始对话（`ptt=true` 按住说话，不跑 VAD）
  - `{"type": "utterance_end"}`：PTT 松手，用攒下的帧开一轮
  - `{"type": "stop"}`：停止对话
  - `{"type": "interrupt"}`：打断当前 turn（半双工下这是唯一打断入口）
  - `{"type": "text", "text": "..."}`：文字输入
  - `{"type": "new_session"}`：换网关会话身份 + turn 递增
  - `{"type": "playback_done", "turn": N}`：客户端把第 N 轮音频播完了
  - `{"type": "set_config", "voice": "...", "tts_rate": "..."}`：切换音色/语速
  - `{"type": "restart"}`：远程重启服务（等价 `POST /api/restart`）

**服务端 → 客户端：**
- 音频二进制：`0x01` + turn 字节 + PCM16（24kHz）
  - turn 字节：当前轮次，打断/新会话后递增，客户端丢弃旧 turn 音频
- 事件消息（JSON，`turn` 字段除 `hello`/`replaced` 外均带上）：
  - `{"type": "hello", "session_id": "...", "voice": "...", "asr_model": "..."}`：连接建立 /
    换会话 / 改配置后回，`session_id` 即本轮起使用的网关会话身份
  - `{"type": "state", "state": "idle|listening|thinking|speaking", "turn": N}`
  - `{"type": "asr_partial", "text": "..."}`：实时字幕（仅流式 ASR 后端有；qwen 批量档无）
  - `{"type": "asr_final", "turn": N, "text": "..."}`：最终识别结果（文字轮不发）
  - `{"type": "agent_delta", "turn": N, "text": "..."}`：LLM 流式增量
  - `{"type": "agent_done", "turn": N, "finish_reason": "..."}`：LLM 生成结束
  - `{"type": "tool_progress", "turn": N, "tool": "...", "label": "...", "emoji": "...", "status": "..."}`
  - `{"type": "tts_sentence", "turn": N, "text": "..."}`：本句开始合成/下发
  - `{"type": "tts_end", "turn": N}`：**本 turn 音频已全部下发**（不是"已播完"）。客户端必须
    等它 + 本地缓冲排空后才回 `playback_done`——早回会让服务端提前回 listening，喇叭还在播，
    自己的 TTS 被录进下一轮
  - `{"type": "replaced"}`：服务端单活跃连接，本页被新页面顶替，应主动退场不再重连
  - `{"type": "restarting"}` / `{"type": "error", "message": "..."}`

## 客户端支持

### 1. 网页版（`web/`）

- **技术栈**：原生 JS + AudioWorklet + WebSocket
- **浏览器**：Safari 14.1+、Chrome 88+
- **特性**：半双工（thinking/speaking 停发麦克风帧）、实时字幕、打断按钮、音色/语速选择、
  附件上传、拖拽投文件、断线自愈
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

**关键配置项**（完整列表见 `server/config.py`；下面按 example 口径写，行末 `▸线上` 是
2026-09-19 核对的真实 `config.yaml` 差异）：

```yaml
# 服务
host: 127.0.0.1        # ▸线上 0.0.0.0（手机走隧道要监听全网卡）
port: 8698
auth_token: ""         # 非空时 /ws 必须带 ?token=（改 0.0.0.0 时务必设置）
                       # 只准写在 gitignore 的 config.yaml 里；0.4.7 起访问日志自动遮成 ***

# Hermes gateway
hermes_base_url: http://127.0.0.1:8647
hermes_model: k3       # 仅占位：实测网关不看 body.model，真正用的模型由 ~/.hermes/config.yaml
                       # 的默认 provider 决定（当前 k3-256k）。换模型要改 Hermes 配置，改这里没用。

# ASR（auto 的优先链：qwen 本地 sidecar > 豆包云端 > sherpa 本地流式 > whisper 批量）
asr_backend: auto      # auto | qwen | doubao | sherpa | whispercpp | faster   ▸线上 doubao

# TTS
tts_backend: auto      # auto=kokoro 本地优先、edge-tts 云端兜底 | kokoro | edge   ▸线上 edge
tts_voice: zh-CN-XiaoxiaoNeural     # edge-tts 音色（kokoro 用 kokoro_voice）
tts_rate: "+0%"
tts_lookahead: 2       # 预合成句数。1 时句间容易断流（上一句播完下一句还没合成）→ 卡顿
playback_grace_ms: 900 # 等客户端 playback_done 的余量（客户端预灌水位 120ms + 网络抖动）

# VAD
vad_threshold: 0.5           # listening 档阈值
vad_end_silence_ms: 1100     # 尾静音判段结束（800 切碎思考停顿，2026-08-11 定 1100）
barge_threshold: 0.85        # ⚠️ 半双工下这几个 barge_* 实际不参与决策（speaking 不处理
barge_hold_ms: 320           #    上行帧，BargeDetector 只在 pre-roll 灌回时被顺带复位）
```

## 依赖管理

**Python 环境**：`py3.13` + venv（`.venv/`）

**ASR 四档（`asr_backend: auto` 的优先链，实测启动日志即按此顺序探测）：**

1. **qwen3-asr 1.7B**（本地 sidecar，优先）
   - 复用 weSaw 的 venv/脚本/权重（`asr_qwen_python` / `asr_qwen_server` / `asr_qwen_model`），
     不在本仓库 pip 里
   - 批量转写：一段话说完才出字，**无实时字幕**（选中它时 `asr_partial` 不会出现）
   - 启动即后台预热（`asr_qwen_warm_at_boot`），否则首次说话要等模型加载；
     `asr_qwen_idle_kill_min>0` 可空闲回收省内存（当前 0=常驻）

2. **doubaoime-asr**（云端流式，次选）
   - 包：`doubaoime-asr`（PyPI）
   - 凭据：`~/.config/doubao-asr/credentials.json`
   - 实时流式、有字幕；**按 WS 连接各建一个实例**（共享单例会互相 reset，见 CHANGELOG 0.4.5）

3. **sherpa-onnx 流式 paraformer**（本地流式，备胎）
   - 包：`sherpa-onnx`
   - 模型：`/Users/gao/clone/expression-trainer/models/sherpa-onnx-streaming-paraformer-bilingual-zh-en`
   - RTF 0.03，边说边出字，与 expression-trainer 共用模型

4. **whisper.cpp / faster-whisper**（本地批处理，兜底）
   - 包：`pywhispercpp`
   - 模型：`models/ggml/ggml-large-v3-turbo-q5_0.bin`（547MB，Metal GPU）
     ▸线上 `asr_ggml_model: models/ggml/ggml-large-v3-q5_0.bin`（1080MB，2026-09-20 冒烟实测 Metal 加载）
   - RTF 0.35，一字不差，首次启动自动下载；约 1s 转写延迟
   - 兜底内部仍先试 whisper.cpp，**只有 `asr_backend: faster` 才跳过它**（0.4.7 修：此前钉 doubao/qwen
     时会绕开本机 ggml 权重直接去拉 faster-whisper）；`models/faster-whisper-<档名>/model.bin` 完整时
     按本地目录加载，不再走 hub 缓存重下

**TTS**：`auto` = kokoro 本地（`models/kokoro/kokoro-v1.1-zh.onnx`）优先、edge-tts 云端兜底；
`kokoro` / `edge` 可强制。edge-tts 偶发失败时整句未出声会重试一次（半句失败不重试，会跳段）。

> ⚠️ **线上 `config.yaml` 实配（2026-09-19 核对）：`asr_backend: doubao` + `tts_backend: edge`**。
> 也就是说 qwen sidecar 与 kokoro 虽然代码在、权重在，当前进程**并未加载**（`pgrep -f qwenasr_server`
> 为空属正常，不是崩溃）。想启用：改配置后 `scripts/restart-service.sh`，qwen 首启预热约 16s。
> 顺带解释了进程看着"很轻"——本地大模型一个都没常驻，长期空闲的进程页还被 macOS 压缩换出了
> （2026-09-20 实测 `ps -o rss` 只剩约 5MB；`ps` 的 VSZ 是地址空间保留，不是占用，别拿它判断）。

**VAD**：silero（pysilero-vad）

**安装注意**：
- pip 镜像：`-i https://mirrors.aliyun.com/pypi/simple/`（直连慢、清华 403）
- HF 权重：hf-mirror + `HF_HUB_DISABLE_XET=1`（Xet 绕镜像 401）

## 验证与测试

```bash
# 冒烟测试（健康检查 + edge-tts + 全链路）
scripts/smoke.sh

# 单元测试（43 项：分句器 / 会话协议时序 / 本轮修复 / 音频 worklet 仿真与变异）
#   音频 worklet 在浏览器外跑不了，tests/worklet_sim.mjs 用假 AudioWorkletProcessor
#   按渲染量子推进来测；小程序端 Player 是另一套实现，用假 WebAudio 上下文 + 假时钟
#   跑它的排度/收口时序；pytest 负责调 node（没装 node 自动 skip）
.venv/bin/python -m pytest tests/ -q

# ASR 延迟基准
.venv/bin/python scripts/bench_asr.py

# WebSocket 协议回归（需服务已启动）
.venv/bin/python scripts/ws_regression.py
```

## 性能指标（M1 Pro 16GB）

⚠️ 下表除注明外均为 2026-08-08 所测，**当时尚未发现跨案串历史**（见「会话身份」节），
数字不能代表修好之后的实况。0.4.7 已于 2026-09-20 15:16 重启上线，重启后的实测见末两行。

| 指标 | 结果 |
|------|------|
| **首包延迟** | 2026-08-08 记 4.10s（ASR 1.0s + Hermes 3.2s + TTS 0.8s）<br>2026-09-19 实测同一句话：干净会话首 delta **4.7s**；串上陈年历史后同轮 **11.8s**（prompt 11801 → 42771 token）<br>2026-09-19 23:14 手机端（旧进程、泄漏未修）：`[turn 1] ★首音 13.12s`、`[turn 2] 首delta 12.17s / ★首音 13.40s` |
| **ASR RTF** | whisper.cpp turbo q5+Metal **0.35**（一字不差）<br>sherpa-onnx paraformer **0.03**（实时流式）<br>qwen3-asr 1.7B 热转写 **RTF≈0.29**；线上实配 doubao 云端，停口→ASR 0.2~4.2s（首轮建连慢） |
| **TTS 合成** | 快于实时（3.8s 音频 1.7s 合成完） |
| **内存占用** | 线上实配（doubao + edge）不常驻本地模型，空闲进程 RSS 仅约 5MB；whisper.cpp 档常驻 ~0.7GB，qwen3-asr sidecar 另算（MPS 独立进程，权重 4.4GB） |
| **WS 回归** | `.venv/bin/python scripts/ws_regression.py`（需服务已启动）；离线单测 43 项（含协议回归 + 音频仿真 + 变异检查；仿真脚本 `node tests/worklet_sim.mjs` 自身 20 条断言：web 两个 worklet 跑在 Node `vm` 里的**真实** process()，小程序 Player 跑假 WebAudio + 假时钟） |
| **WS 回归（2026-09-20 15:16，0.4.7 新进程 PID 96226）** | `scripts/ws_regression.py` **9/9 通过**：语音 turn 全事件序 ✓、打断后 turn 递增且迟到帧 0 ✓；本轮 `[turn 1] 停口→ASR 1.02s / 首delta 13.83s / ★首音 14.94s / 完 16.64s` |
| **串历史修复的网关侧坐实（2026-09-20）** | 三条 hermes-live 会话全为 `hl-<uuid16>`、首话轮 `history=0`、`in=1178x` token（修复前同一场景被灌到 **42,771**）；同连接第二话轮 `history=2`、`in=12021` → **同一段对话仍然连续，不是改成失忆**。⚠️ 但首 delta 并没有因此变快：同时段 11.8k prompt 的 API `latency` 实测 12.7s / 15.2s，而凌晨 CLI 跑同量级 prompt 只要 4.5~5.2s —— 大头是 k3-256k 池子白天的抖动，不是历史体积。修历史只保证了"越聊越慢"不会再来 |
| **冒烟（2026-09-20 01:04，含 0.4.7 修复的批量兜底路径）** | `scripts/smoke.sh` 4/4 通过，全程 **36s**：`cli.m0_pipeline --wav` → whisper.cpp 加载本地 `ggml-large-v3-q5_0.bin`（1080MB，MTL0）→ 停口→ASR **1.61s** → 首delta 7.25s → ★首音 8.70s。**修前**这一步是去 hf-mirror 重下 3 GB faster-whisper，7 分钟无进度后手杀（见 CHANGELOG 0.4.7）；⚠️ 走的是本机 CLI 直连，不经浏览器播放，故首音不含下行缓冲/预灌那部分时间 |

**延迟大头**：Hermes 首 delta。旧结论"agent 大 prompt 预填充 + 池代理慢"只对了一半——
真正放大它的是被内容指纹复活的历史会话；串历史修掉后，剩下的才是 prompt 体积本身。

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
- 标签：每次小程序上传打 tag（如 `v0.4.7`）。⚠️ 2026-09-20 核对：本地仓库 `git tag` **为空**，
  这条约定一直没落地，下次上传顺手补 `v0.4.7`

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
node scripts/wx-upload.js 0.4.7 "说话中可打断 + 音频收口修复"

# 登录微信公众平台设为体验版/提交审核
# https://mp.weixin.qq.com
```

⚠️ 0.4.7 + 0.4.8 的小程序端改动（打断按钮、socket 生命周期、`endTurn` turn 守卫、
**同 turn `setTurn` 幂等**）**尚未上传**；仓库里查不到最后一次上传用的是哪个版本号（无 tag、无记录），
以微信公众平台上的当前版本为准。
服务端改动同理需重启才生效（0.4.8 待重启项：`Cache-Control: no-cache`、合成失败补发 `tts_end`、
`closed` 后丢上行、`playback_done` 日志）。

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
2. ~~跨案串历史~~（2026-09-19 已修：不带 `X-Hermes-Session-Id` 时网关按
   `sha256(system_prompt+首句)` 派生会话身份，固定开场白会复活陈年会话；现**每条 WS 连接**
   铸造 `hl-<uuid16>`，「新会话」再轮换一次。
   顺带解释了"首包延迟偶发 10s+"——被捞回的历史把 prompt 撑到 4 万 token）
3. ~~语音卡顿 / 回答说到一半就断~~（2026-09-19 已修：`tts_lookahead` 1→2、播放 worklet 加
   120ms 预灌水位、下行收口改由服务端 `tts_end` 触发而非客户端缓冲欠载猜测。
   2026-09-20 15:16 已重启上线、`ws_regression.py` 9/9 通过；**真人试听仍待高律师手机确认**）
   - **0.4.7 重启后紧接着出现「手机网页整场没声音」**，0.4.8 已定位并修好（详见 CHANGELOG）：
     下行块从「预切缓冲」改成「整块 + offset」，而 `app.js` 与 worklet 分别缓存 → 新旧混合时
     每块解成 0 个样本，**麦克风/ASR 全正常却一句不听**。现 worklet 容忍无 offset、
     同 turn 重复 `setTurn` 幂等，另加 `Cache-Control: no-cache` 中间件堵住混合缓存这条路。
     ⚠️ 前端 JS 改动已在盘上、由当前进程直接生效（实测 curl 到的字节与磁盘一致），
     **重启只是为了让 `Cache-Control` 头生效**——这一条待高律师点头
   - ⚠️ **仍未证实的部分**：以上是我用离线仿真能复现并修掉的机制，不等于他手机上那次静音的唯一成因
     （设备侧复听还没回话）。为这条我补了 `playback_done` 的服务端日志——下次再静音，
     日志会直接说明「客户端到底播没播」，而不是靠猜
4. 首包延迟里仍有 Hermes prompt 体积本身的部分（干净会话首 delta ~4.7s）
5. edge-tts 偶尔被风控（概率低，备胎 `say -v Tingting` 可用）
6. thinking/speaking 态半双工丢帧（Hermes 思考期说话被丢弃）——高律师 2026-08-11 定**不改**：
   排队下一轮的方案经常出 bug，维持半双工
7. **`auth_token` 已经外泄过，建议轮换**：两件事叠加
   - 0.4.7 之前的访问日志把 `?token=…` 明文写进 `logs/service.log`（2026-09-19 的日志行即例），
     现已遮罩成 `token=***`，但**旧日志里那份还在**（文件 gitignored，机器上谁都能读）
   - 更要紧的：这个 token 曾**随仓库提交**——`git log -S` 命中 `eef9761`（0.3.2 内置默认 token）、
     `44f7a4e`（0.4.5 又内置一次），到 `40b139b` 才移除。origin 是 GitHub 私有仓，
     历史里的那份即使仓库私有也仍在，改历史代价大
   → 干净的收口只有一条：**换一个新 token**（改 `config.yaml` → 重启 → 小程序设置与网页
   `localStorage.hl_token` 重填），并清掉旧 `logs/service.log`。2026-09-20 核对，尚未执行

### 待办事项

- [x] 0.4.7 重启上线（2026-09-20 15:16，PID 96226）+ `scripts/ws_regression.py` 9/9 已通过
- [ ] 手机实听确认卡顿消失（只能真人耳朵判；半双工收口、预灌水位这些仿真测不出主观听感）
- [ ] **轮换 `auth_token`**（见已知问题 7：它进过 git 历史，也进过 0.4.7 之前的明文访问日志），
      与上一条一起做完：换 token → 重启 → 跑回归 → 清旧日志 → 手机/网页重填
- [ ] 首包延迟优化（考虑 Hermes agent prompt 精简）
- [ ] TTS 备胎方案完善（Piper 本地合成）
- [ ] 小程序增加历史会话列表
- [ ] iOS App 推送通知支持

## 联系与支持

- **开发者**：高城斌律师
- **用途**：五邑律师事务所内部工具
- **协议**：内部项目，未开源

## 工作纪要

### 2026-09-20 18:33 手机端「没有声音」二次复现与缓存破壁（Hermes）
- 现象：17:42 重启（0.4.8 生效）后，18:27 高律师 iPhone Safari 测「在吗」——ASR 正常、服务端 metrics 报首音 4.20s，但**无 playback_done**，客户端全程无声。
- 判定：服务端已在下发（首音有值），静音在客户端播放侧；指向 Safari 对 `addModule` 模块的启发式缓存——no-cache 响应头只在回源时生效，已装模块可直接复用旧实例（0.4.7 半新半旧错配的同类机制）。
- 处置：`web/audio.js` 两个 worklet URL 与 `web/app.js`/`index.html` 引用统一加 `?v=0.4.8` 版本破壁（版本一变 URL 即变，无需用户清缓存）；18:33 重启服务，`/web/audio.js?v=0.4.8` 实测 200 + no-cache + 新代码在served。
- **已验（18:42 高律师）：手机端恢复有声**——确认根因=客户端缓存旧 worklet；`?v=` 版本破壁后重开页面即好。屏上诊断计数（帧数/cv状态/P发R播）留存，状态行可见，下次再出问题一眼定位。
