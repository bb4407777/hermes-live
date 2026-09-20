# Hermes-live

用语音和本机 Hermes gateway 实时对话（参照 gpt-live 的形态，语音层全免费自建）。

```
浏览器/小程序 ──ws://127.0.0.1:8698──> hermes-live 服务
   │ AudioWorklet/RecorderManager 16k PCM16  │ silero VAD 分段
   │ AudioWorklet/WebAudio 24k                │ qwen3-asr 本地 sidecar（优先，无实时字幕）
   │   （turn 字节打断过滤）                     │ doubao 云端流式 / sherpa-onnx / whisper.cpp
   └── 半双工：thinking/speaking 停发麦克风      │ Hermes 8647 /v1/chat/completions（SSE 流式）
       打断只有「打断」按钮                   │ 中文分句 → kokoro 本地 / edge-tts 逐句合成
                                             └→ 音频帧流回客户端
```

**当前版本：0.4.8**（2026-09-20）
- 会话身份：每进程随机铸造 `X-Hermes-Session-Id`，杜绝网关按"开场白内容指纹"复活陈年历史
  （律师场景 = 跨案串内容；也是"首包偶发 10s+"的真因）
- 语音卡顿：`tts_lookahead` 1→2、播放端 120ms 预灌水位、收口改由服务端 `tts_end` 触发
- 小程序全功能：实时字幕、音色/语速、新会话、打断、远程重启服务
- ASR 四档：qwen3-asr 本地（优先）→ 豆包云端流式 → sherpa-onnx 本地流式 → whisper.cpp 兜底
- 半双工对话：AI 说话时不收麦克风，靠「打断」按钮停（2026-08-11 定，见 PROJECT.md）

## 启动

### 服务端

```bash
cd ~/Code/hermes-live
launchctl kickstart -k gui/$(id -u)/com.gaochengbin.hermes-live   # 正式跑：launchd 常驻
scripts/restart-service.sh                                        # 重启（自动识别 launchd）
.venv/bin/python -m server.main                                   # 调试：前台跑（先 bootout）
```

服务已在 2026-08-11 起改 launchd 常驻（KeepAlive）：Mac 重启后隧道还活着，非常驻的话
8698 死了、手机只会拿到 502。

### 客户端

**1. 网页版（推荐本机使用）**

浏览器（Safari/Chrome 均可）打开 `http://127.0.0.1:8698`，点「开始对话」授权麦克风即聊。
按需启动、用完 Ctrl+C 停，不做常驻（16GB 内存让步）。

**2. 微信小程序（推荐手机使用）**

- 扫码进入「哈尔密斯语音对话」小程序（appid: `wxa5b11d3d8b80b07b`）
- 首次使用：点右上角「⚙️」设置服务器地址和 token
  - 服务器地址：`ws://你的IP:8698`（需 Mac 侧 `config.yaml` 改 `host: 0.0.0.0`）
  - Token：`config.yaml` 里设的 `auth_token`（必须设置，安全要求）
- 功能：实时字幕、音色/语速选择、新会话、**远程重启服务**
- 上传脚本：`node scripts/wx-upload.js [版本号] [描述]`

**3. iOS 原生 App**

参见 `ios/README-ios.md`，免费个人签真 app，工程已生成好，装 Xcode 后双击 `ios/HermesLive.xcodeproj` 即可跑。

**4. Tailscale 远程访问**

见 `docs/mobile.md`（网页零开发方案）。

~~菜单栏版~~（2026-08-09 已弃用删除）：WKWebView 壳缓存页面不及时，直接用网页版即可。
给网关提速的配置已落待重启：`docs/hermes-toolset-proposal.md`。

- 配置：`cp config.yaml.example config.yaml` 后改（音色/ASR 档位/VAD 阈值等，全部键见 `server/config.py`）。
- 终端模式（无浏览器）：`.venv/bin/python -m cli.m0_pipeline --mic`（半双工；终端 App 需麦克风权限）。
- 会话：与微信通道天然隔离（platform=api_server 独立会话空间）；「新会话」按钮重开；页面刷新不断会话。

## 验证

```bash
scripts/smoke.sh                          # 冒烟：健康检查 + edge-tts + 全链路（≤2 次 LLM 调用）
.venv/bin/python -m pytest tests/ -q      # 43 项：分句器 + 协议时序 + 本轮修复 + worklet 仿真/变异
.venv/bin/python scripts/bench_asr.py     # ASR 延迟基准（0 次 LLM）
.venv/bin/python scripts/ws_regression.py # WS 协议回归（需服务已启动，2 次 LLM 调用）
```

实测数据（M1 Pro 16GB，2026-08-08）：见文末「实测记录」。

## 红线备忘（对 Hermes gateway 只读）

- **绝不**重启/修改 gateway（launchd 常驻，PID 见 `pgrep -f hermes_cli.main`）；只调它的 HTTP 接口。
- **绝不** pip 安装/升级 hermes-agent（系统 site-packages 有本地补丁，升级即抹掉）。
- state.db 只读不碰；8642 端口是 qclaw_launcher 的，不碰。
- 语音打断的实现 = 断开 SSE 连接，网关会自己 `agent.interrupt()`（api_server.py 内建行为），不需要也不允许更重的手段。

## 设计要点

- WS 协议：音频二进制（`0x01`+PCM16 上行；`0x01`+turn 字节+PCM16 下行），控制走 JSON。turn 字节让打断后的在途旧音频被客户端自然丢弃，无需清空握手。
- 状态机 `idle→listening→thinking→speaking`：thinking/speaking 忽略麦克风（半双工，
  2026-08-11 定：排队下一轮的方案常出 bug）；打断走「打断」按钮 → turn+1 + 取消 SSE，
  在途旧音频帧被客户端按 turn 字节丢弃。`barge_*` 配置项在半双工下实际不参与决策。
- 分句器带首句加速（≥10 字遇逗号即切）压首包延迟；markdown/代码块清洗后再进 TTS。
- 每轮请求附带 system 消息注入「口语化短句」风格（网关将其临时叠加，不改 Hermes 配置）。
- 前端只用 getUserMedia / WebSocket / AudioWorklet（Safari 14.1+），iOS 壳同套页面回环托管复用。

## 依赖与备胎

> ⚠️ 下面是**代码支持的四档**。**线上 `config.yaml` 实配是 `asr_backend: doubao` + `tts_backend: edge`**
> （2026-09-19 核对），所以 qwen sidecar 进程当前没跑、kokoro 也没加载——不是坏了，是没配。
> 想切 qwen：改 `asr_backend: qwen`（或 `auto`）后重启，首启预热约 16s。

全部 py3.13 + Apple Silicon 预编译轮子（见 `requirements.txt`），明确不装 torch/mlx。

**ASR 四档（`asr_backend: auto` 的优先链）：**
1. **qwen3-asr 1.7B**（默认优先）：本地 sidecar，复用 weSaw 的 venv/脚本/权重（不在本仓库 pip 里）
   - 批量转写，一段话说完才出字 → **该档下没有实时字幕**；启动即后台预热
2. **doubaoime-asr**（云端流式）：豆包输入法逆向 ASR，实时流式，准确度高
   - 需联网，凭据：`~/.config/doubao-asr/credentials.json`（用 doubaoime-asr 包登录生成）
3. **sherpa-onnx**（本地流式备胎）：流式 paraformer，RTF 0.03，边说边出字
   - 模型与 expression-trainer 共用：`/Users/gao/clone/expression-trainer/models/sherpa-onnx-streaming-paraformer-bilingual-zh-en`
4. **pywhispercpp**（最终兜底）：whisper.cpp turbo q5_0，Metal GPU，RTF 0.35
   - 模型：`models/ggml/ggml-large-v3-turbo-q5_0.bin`（547MB，首次启动自动下载）

**TTS：**
- 主力：kokoro 本地（`models/kokoro/kokoro-v1.1-zh.onnx`，`tts_backend: auto` 优先）
- 兜底：edge-tts（免费微软音色，单句合成快于实时）
- 备胎：`say -v Tingting`（离线）或 Piper
- 接口：`server/tts.py` 是唯一接口点

安装注意（本机网络实测）：pip 走 `-i https://mirrors.aliyun.com/pypi/simple/`（直连 PyPI 极慢、清华 403）；
HF 权重走 hf-mirror 且必须 `HF_HUB_DISABLE_XET=1`（Xet CAS 绕过镜像会 401，asr.py 已内置这两个默认值）。

## 实测记录（M1 Pro 16GB，2026-08-08 验收）

⚠️ 本表是 0.4.x 早期验收快照。其中两项已被后续实况推翻：
① 当时网关的实际模型是 `pool-deepseek-v4-flash`，**2026-09-19/20 复核已是 `k3-256k`**
（`~/.hermes/config.yaml` 的 `model.default` + `agent.log` 坐实；请求 body 的 `model` 字段始终不参与选型）；
② 4.10s 首包是在"会话历史被内容指纹复活"这个问题存在的期间测的，修好之后需复测
（2026-09-19 对照：同一句话干净会话首 delta 4.7s，串上历史后 11.8s）。

| 项 | 结果 |
|---|---|
| ASR 基准（2.9s 中文） | whisper.cpp turbo q5+Metal **RTF 0.35**（转写一字不差）；faster-whisper turbo int8 RTF 1.19（慢，弃）；small RTF 0.41（有错字，弃） |
| M0 语音全链路 | 停口→首音 **4.10s**（ASR 1.0s + Hermes 首 delta 3.2s + TTS 首包 0.8s），验收线 5s 内 |
| 延迟大头 | Hermes 首 delta 3-4s。**网关实际模型 2026-09-19/20 复核为 `k3-256k`**（`~/.hermes/config.yaml` 的 `model.default` + `agent.log` 的 `API call #N: model=k3-256k` 坐实；08-08 当时记的 `pool-deepseek-v4-flash` 已过期，请求 body 的 `model` 字段始终不参与选型）——首字慢在 Hermes agent 大 prompt 预填充+池代理，不在模型档位；会话变热后 3.9s→3.2s（前缀缓存）。edge-tts 单句合成快于实时（3.8s 音频 1.7s 合成完） |
| WS 回归（scripts/ws_regression.py） | 9/9：语音 turn 全事件序 ✓；打断后 turn 递增、旧音频零迟到帧 ✓；网关日志坐实 `SSE client disconnected; interrupted agent task`（2026-08 那次跑的是旧进程；**2026-09-20 15:16 已在 0.4.7 新进程上重跑，仍 9/9 通过**，网关侧同时坐实 `hl-` 会话 + `history=0` + prompt 回到 11.8k） |
| 单测 | tests/test_sentencer.py 9/9 → 2026-09-19 扩到 39、2026-09-20（0.4.8）到 **43 项**：服务端协议时序 + 本轮修复 + `tests/worklet_sim.mjs` 仿真（脚本自身 20 条断言，跑在 Node `vm` 里的真实 worklet）+ **12 条变异检查**（把 worklet 逐条改坏，仿真必须红）。`pytest tests/ -q` 全绿（仿真要 node，没装则自动 skip） |
| 冒烟（scripts/smoke.sh，2026-09-20 01:04） | 4/4 通过、全程 36s：gateway 健康 ✓ → `/api/health` ✓ → edge-tts ✓ → `cli.m0_pipeline --wav` 全链路 ✓（whisper.cpp 走本机 ggml 权重，停口→ASR 1.61s、首delta 7.25s、★首音 8.70s）。此前 4/4 卡在批量兜底绕开 ggml 去重下 3 GB faster-whisper，7 分钟无进度（已修，见 CHANGELOG 0.4.7）；⚠️ 该步是本机 CLI 直连、不经浏览器播放 |
| 服务足迹 | 线上 launchd 常驻进程空闲 RSS **约 5MB**（实配 `asr_backend: doubao` 云端 + `tts_backend: edge`，本机不常驻大模型）；whisper.cpp/qwen 档才会常驻 0.7~4.4GB。停服走 `launchctl bootout gui/$(id -u)/com.gaochengbin.hermes-live`，旧的 `kill $(cat tmp/server.pid)` 已废弃 |
| 修复前实况（2026-09-19 23:14，手机走隧道） | `[turn 1] ★首音 13.12s`、`[turn 2] 首delta 12.17s / ★首音 13.40s / 完 42.60s`——旧进程 + 上下文泄漏未修时的实测；0.4.7 重启后同口径复测 |
