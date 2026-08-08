# Hermes-live

用语音和本机 Hermes gateway 实时对话（参照 gpt-live 的形态，语音层全免费自建）。

```
浏览器开麦 ──ws://127.0.0.1:8698──> hermes-live 服务
   │ AudioWorklet 采集 16k PCM16          │ silero VAD 分段
   │ AudioWorklet 播放 24k（turn 过滤）    │ faster-whisper 本地转写（中文）
   └── 全双工，播音时高门槛 VAD 打断        │ Hermes 8647 /v1/chat/completions（SSE 流式）
                                          │ 中文分句 → edge-tts 逐句合成（免费微软音色）
                                          └→ 音频帧流回浏览器
```

## 启动

```bash
cd ~/Code/hermes-live
.venv/bin/python -m server.main          # → http://127.0.0.1:8698（首次会下载 whisper 权重）
```

浏览器（Safari/Chrome 均可）打开 `http://127.0.0.1:8698`，点「开始对话」授权麦克风即聊。
按需启动、用完 Ctrl+C 停，不做常驻（16GB 内存让步）。

- 配置：`cp config.yaml.example config.yaml` 后改（音色/ASR 档位/VAD 阈值等，全部键见 `server/config.py`）。
- 终端模式（无浏览器）：`.venv/bin/python -m cli.m0_pipeline --mic`（半双工；终端 App 需麦克风权限）。
- 会话：与微信通道天然隔离（platform=api_server 独立会话空间）；「新会话」按钮重开；页面刷新不断会话。

## 验证

```bash
scripts/smoke.sh                          # 冒烟：健康检查 + edge-tts + 全链路（≤2 次 LLM 调用）
.venv/bin/python -m pytest tests/ -q      # 分句器单测
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
- 状态机 `idle→listening→thinking→speaking`：thinking 忽略麦克风（防噪声误取消）；speaking 全双工，VAD 高门槛档（概率≥0.85 持续 320ms）触发打断，pre-roll 500ms 保住打断句开头。
- 分句器带首句加速（≥10 字遇逗号即切）压首包延迟；markdown/代码块清洗后再进 TTS。
- 每轮请求附带 system 消息注入「口语化短句」风格（网关将其临时叠加，不改 Hermes 配置）。
- 前端只用 getUserMedia / WebSocket / AudioWorklet（Safari 14.1+），为 M3 菜单栏 WKWebView 壳预留：
  NSStatusItem + NSPopover 内嵌 WKWebView 指向 8698，`NSMicrophoneUsageDescription` + audio-input entitlement +
  `WKUIDelegate.requestMediaCapturePermissionFor` 回 `.grant`；免费 Xcode CLT + ad-hoc 签名即可（同 gpt-live build-app.sh 做法），无需付费开发者账号。

## 依赖与备胎

全部 py3.13 + Apple Silicon 预编译轮子（见 `requirements.txt`），明确不装 torch/mlx。
edge-tts 若被风控：`server/tts.py` 是唯一接口点，备胎 `say -v Tingting`（离线）或 Piper（onnx 本地）。
whisper 档位：默认 `large-v3-turbo`（int8，约 1.2GB 内存），`config.yaml` 里 `asr_model: small` 可省到 0.4GB。

## 实测记录

（M0/M1 验收时回填）
