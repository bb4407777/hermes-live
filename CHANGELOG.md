# Changelog

## [0.4.6] - 2026-08-10

### Security
- **隧道流量强制 token 校验**：cloudflared 经本机回环转发，此前被 `_check_token` 回环豁免整体放行，`live.gaochengbin.com` 实际无密码暴露公网；现对带 `Cf-Ray` 头的请求强制 token（`?token=` 与 `Authorization: Bearer` 均认），公网无 token 实测 401

### Fixed
- `_check_token` 接受 Bearer 头（此前只认 query，重启按钮在 LAN 下会 401）

## [0.4.5] - 2026-08-10

### Changed
- **token 内置为小程序默认值**：沿用 0.3.2 默认 token 思路，首次使用免手填；服务端 `auth_token` 保持不变；设置弹窗只剩服务器地址一项（依据：体验版仅本人账户可登，高律师确认）

## [Unreleased]

### Fixed
- **asr_backend 分流错误**：`whispercpp`/`faster` 档此前会被 `!= "doubao"` 分支拦截、实际仍加载 sherpa；现仅 `auto`/`sherpa 才走 sherpa 流式，批量 ASR 配置真正生效
- **doubao ASR 空结果**：实测 session 每次立即结束（result=''），暂弃用，配置切回 faster-whisper

### Changed
- **恢复流式朗读**：SSE 边生成边分句送 TTS，回退 0.4.3 的「等全量返回再统一入队」（保留禁 barge-in）；实测首句 3.74s / 首音 5.0s / 整轮播完 37.06s（高律师 2026-08-10 定）
- **ASR 切回原始 large-v3，最终落 whisper.cpp Metal**：`asr_backend: whispercpp` + `models/ggml/ggml-large-v3-q5_0.bin`（ModelScope `timeless/whispercpp` 下载，HF xet CDN 本机直连超时）；实测 RTF 0.47（faster-whisper CPU int8 为 1.37，约 3 倍提速）
- faster-whisper large-v3 权重（`models/faster-whisper-large-v3`，ModelScope `keepitsimple/faster-whisper-large-v3`）保留作兜底
- **替换应用图标**：使用 `~/Downloads/9154.png`（640×640 RGBA 黑白漫画风人像）作主图，`scripts/gen_icons.py` 一键重出 macOS iconset（10 档 RGBA）+ iOS 1024（白底 RGB，无 alpha 满足 App Store）+ xcassets 三处源 + 网页 favicon（`web/favicon.png`，`index.html` 挂 `<link rel="icon">`）；旧 `assets/icon-source.jpg` 进回收站
- **token 不再硬编码入库**：`miniprogram/app.js` 移除默认内置 auth_token，只从 `wx.getStorageSync('hl_token')` 读取；服务端 token 仍仅存 gitignored `config.yaml`（2026-08-11 定：禁随仓库外泄；老用户手机存储已缓存，不受影响）

## [0.4.4] - 2026-08-10

### Added
- **小程序远程重启服务**：设置页增加「重启」按钮，可在手机端重启服务端（便于调试和应用更新）
- **服务端 `/api/restart` 接口**：收到请求后延迟 1 秒返回响应，然后 `os.execv()` 重启自己

### Fixed
- **doubao ASR 调试日志**：追踪 session 启动与错误，便于诊断连接问题

## [0.4.3] - 2026-08-10

### Changed
- **简化对话模式**：全量输入→全量输出，禁用 barge-in（AI 说话时不再主动打断）
- 适合长篇回答场景，避免频繁打断导致内容不完整

## [0.4.2] - 2026-08-10

### Added
- **顶栏固定**：标题和控制按钮固定在顶部，滚动时不遮挡
- **实时字幕移入对话流**：识别中间结果以浅色气泡显示在消息列表中

### Changed
- 界面布局优化，对话记录更清晰

## [0.4.1] - 2026-08-09

### Changed
- **恢复免提自动听讲**：关掉按住说话模式，恢复全双工自动监听
- **修复小程序"只会应答"问题**：服务端增加 `tts_end` 消息，与 0.4.1 协议匹配

### Fixed
- 服务端旧进程缺 `tts_end` 导致每轮卡住的问题

## [0.4.0] - 2026-08-09

### Added
- **豆包云端 ASR**（doubaoime-asr）：实时流式，准确度高，优先级最高
  - 需联网，凭据：`~/.config/doubao-asr/credentials.json`
  - 降级链：doubao（云端）→ sherpa（本地流式）→ whispercpp（本地批处理）
- **按住说话（PTT）模式**：小程序增加按钮，按住录音、松开发送

### Fixed
- **播放卡顿根治**：修复音频播放串音和欠载问题

## [0.3.4] - 2026-08-09

### Fixed
- **播放串音修复**：playback_done 欠载检测从 350ms 调至 800ms，开录延迟 500ms

## [0.3.3] - 2026-08-09

### Changed
- **撤销默认 token**：首次使用须手动设置服务器地址和 token（安全考虑）

## [0.3.2] - 2026-08-09

### Added
- **实时字幕**：说话时同步显示识别中间结果
- **默认 token**：小程序内置默认 token，首次使用更方便

## [0.3.1] - 2026-08-09

### Changed
- **半双工模式**：AI 说话时停止录音防回声，结束后恢复录音

## [0.3.0] - 2026-08-09

### Added
- **小程序首版**：微信小程序支持，UI 对齐网页版
  - 自动开聊：进入即开始监听
  - 音色/语速选择器：实时切换，无需重启
  - 新会话按钮：清空历史，重新开始
- **流式实时字幕**：边说边显示识别结果（`asr_partial` 事件）

### Changed
- **ASR 换 sherpa-onnx**：流式 paraformer，RTF 0.03，边说边出字
  - 替代 whisper.cpp 批处理模式，延迟从 1.0s 降至 <100ms
  - 模型与 expression-trainer 共用，无需额外下载

## [0.2.1] - 2026-08-08

### Added
- **Cloudflare Tunnel**：wss://live.gaochengbin.com → 本机 8698（外网访问方案）

### Fixed
- **连接报错优化**：显示实际地址和断连原因
- **三个语音问题修复**：
  - 幻听过滤：`asr_no_speech_prob_max` 从 0.6 收紧至 0.5
  - VAD 截断：`vad_end_silence_ms` 从 600ms 延长至 1200ms（中文停顿普遍超 600ms）
  - 识别准确度：ASR initial_prompt 增加律所场景词汇引导

## [0.2.0] - 2026-08-08

### Added
- **全双工对话**：AI 说话时可随时打断（高门槛 VAD：概率≥0.85 持续 320ms）
- **turn 字节过滤**：打断后的在途旧音频被客户端自然丢弃，无需清空握手
- **首句加速**：分句器 ≥10 字遇逗号即切，压首包延迟
- **pre-roll 机制**：打断句开头回补 500ms，保住完整语义

### Changed
- **状态机优化**：`idle→listening→thinking→speaking`
  - thinking 态忽略麦克风（防噪声误取消）
  - speaking 态全双工，VAD 高门槛档触发打断

## [0.1.0] - 2026-08-07

### Added
- **初版发布**：网页版语音对话
  - whisper.cpp turbo q5_0（Metal GPU，RTF 0.35）
  - silero VAD 分段
  - edge-tts 逐句合成
  - Hermes gateway 对话后端
- **验证套件**：
  - `scripts/smoke.sh`：冒烟测试（健康检查 + 全链路）
  - `scripts/bench_asr.py`：ASR 延迟基准
  - `scripts/ws_regression.py`：WS 协议回归
  - `tests/test_sentencer.py`：分句器单测

### Performance
- **首包延迟 4.10s**（M1 Pro 16GB）：ASR 1.0s + Hermes 首 delta 3.2s + TTS 首包 0.8s
- **ASR RTF 0.35**：whisper.cpp turbo q5+Metal，2.9s 中文音频转写一字不差
