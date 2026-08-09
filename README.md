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

~~菜单栏版~~（2026-08-09 已弃用删除）：WKWebView 壳缓存页面不及时，直接用网页版即可。
手机上用两条路：`docs/mobile.md`（Tailscale + 网页零开发）或 `ios/README-ios.md`（免费个人签真 app，
工程已生成好，装 Xcode 后双击 `ios/HermesLive.xcodeproj` 即可跑；Mac 侧需 config.yaml 配
`host: 0.0.0.0` + `auth_token`）。给网关提速的配置已落待重启：`docs/hermes-toolset-proposal.md`。

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
- 前端只用 getUserMedia / WebSocket / AudioWorklet（Safari 14.1+），iOS 壳同套页面回环托管复用。

## 依赖与备胎

全部 py3.13 + Apple Silicon 预编译轮子（见 `requirements.txt`），明确不装 torch/mlx。
ASR 双后端（`server/asr.py`，`asr_backend` 可切）：**默认 pywhispercpp（whisper.cpp turbo q5_0，Metal GPU）**，
faster-whisper（CPU int8）兜底。edge-tts 若被风控：`server/tts.py` 是唯一接口点，备胎 `say -v Tingting`（离线）或 Piper。

安装注意（本机网络实测）：pip 走 `-i https://mirrors.aliyun.com/pypi/simple/`（直连 PyPI 极慢、清华 403）；
HF 权重走 hf-mirror 且必须 `HF_HUB_DISABLE_XET=1`（Xet CAS 绕过镜像会 401，asr.py 已内置这两个默认值）。
权重位置：`models/ggml/ggml-large-v3-turbo-q5_0.bin`（547MB，主力）+ `models/` 下 faster-whisper turbo（1.5GB，兜底，可删）。

## 实测记录（M1 Pro 16GB，2026-08-08 验收）

| 项 | 结果 |
|---|---|
| ASR 基准（2.9s 中文） | whisper.cpp turbo q5+Metal **RTF 0.35**（转写一字不差）；faster-whisper turbo int8 RTF 1.19（慢，弃）；small RTF 0.41（有错字，弃） |
| M0 语音全链路 | 停口→首音 **4.10s**（ASR 1.0s + Hermes 首 delta 3.2s + TTS 首包 0.8s），验收线 5s 内 |
| 延迟大头 | Hermes 首 delta 3-4s。**网关实际模型已是 pool-deepseek-v4-flash**（agent.log 坐实；k3 仅 fallback，`/v1/models` 报的 "k3" 是陈旧别名，请求 model 字段不参与选型）——首字慢在 Hermes agent 大 prompt 预填充+池代理，不在模型档位，换模型无收益；会话变热后 3.9s→3.2s（前缀缓存）。edge-tts 单句合成快于实时（3.8s 音频 1.7s 合成完） |
| WS 回归（scripts/ws_regression.py） | 9/9：语音 turn 全事件序 ✓；打断后 turn 递增、旧音频零迟到帧 ✓；网关日志坐实 `SSE client disconnected; interrupted agent task` |
| 单测 | tests/test_sentencer.py 9/9 |
| 服务足迹 | whisper.cpp 常驻约 0.7GB；按需启动，停服 `kill $(cat tmp/server.pid)` |
