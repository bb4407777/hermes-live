# hermes-live

**一行命令，和你的 LLM 实时语音对话。** 浏览器开麦即聊：本地 silero VAD 断句、whisper 本地识别、
免费 edge-tts 合成，接任意 OpenAI 兼容端点（本地 gateway、ollama/vllm 前的兼容层、云端 API 均可）。
支持随时开口打断（barge-in：断开 SSE，上游立即停止生成不空烧 token）。

```bash
npx hermes-live --api-url http://127.0.0.1:8647 --api-key sk-xxx --model k3
```

首次运行自动完成：建 Python venv → 装依赖 → 下 whisper 权重（574MB）→ 起服务 → 打开浏览器。
之后每次秒起。数据都在 `~/.hermes-live/`（含 `config.yaml`，改配置编辑它）。

```
浏览器开麦 ──ws──> 本地服务 ──SSE──> 你的 OpenAI 兼容端点
 AudioWorklet 16k    silero VAD 分段
 24k 播放(打断过滤)  whisper.cpp 识别(Metal/CPU)
                     edge-tts 流式合成（免费）
```

## 常用选项

| 选项 | 说明 |
|---|---|
| `--api-url` / `--api-key` / `--model` | 你的 OpenAI 兼容端点三件套 |
| `--mirror` | 中国大陆网络：pip 走阿里云镜像、权重走 hf-mirror |
| `--skip-model` | 跳过 whisper.cpp 权重（改用 faster-whisper 自动下载） |
| `--host 0.0.0.0 --token <随机串>` | 手机/局域网接入（带鉴权门，回环免 token） |
| `--port` / `--no-browser` | 端口 / 不自动开浏览器 |

## 要求

- Python ≥ 3.10（系统里有即可，其余全自动）
- Node ≥ 16、curl
- macOS（Apple Silicon 走 Metal 加速，实测停口→出声 ~4s）；Linux/Windows 理论可用（CPU 识别，未充分测试）

## 说明

- 语音全链路本地/免费：无 OpenAI Realtime 之类的按分钟计费，唯一的 LLM 消耗是你自己的端点。
- 打字也行：页面有文本框与附件拖拽（文件落盘、路径回填，端点侧用工具自取）。
- edge-tts 使用微软在线免费接口，音色可在页面切换（晓晓/云希等）。

MIT © bb4407777
