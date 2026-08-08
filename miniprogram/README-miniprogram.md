# 哈尔密斯 · 微信小程序端

原生小程序（**不是** web-view 套网页——小程序 web-view 里 getUserMedia 麦克风不可用），
与 Mac 上 hermes-live 服务端协议完全一致：JSON 控制消息 + 1 字节前缀二进制音频帧。
录音 `RecorderManager`（16k PCM 分帧，缓冲重切 1024B 对齐服务端 VAD），播放 WebAudio（24k，turn 过滤防打断竞态）。

## 账号现状（2026-08-08 后台核实）

- 小程序「哈尔密斯」已注册，AppID `wxa5b11d3d8b80b07b`（已填进 `project.config.json`），基本信息与类目已完成，未传过代码。
- **备案平台审核中——不挡开发**（官方口径：备案可与开发并行，上架发布前完成即可；主体有已备案小程序可复用资料加速）。
- 微信认证未做：只影响被搜索/被分享，自用无所谓。

## 三档路线（按投入排序）

### 档 1：今天就能用（开发版，零额外成本）
1. Mac 装[微信开发者工具](https://developers.weixin.qq.com/miniprogram/dev/devtools/download.html)，微信扫码登录（你是管理员）。
2. 导入项目：目录选 `~/Code/hermes-live/miniprogram`，AppID 自动带出。
3. 手机微信扫「预览」二维码打开开发版 → 右上角 … → **打开「开发调试」**（跳过 wss 合法域名校验，此路线的关键一步）。
4. 点「设置」填服务器地址：
   - 同一 WiFi：`ws://<Mac 内网 IP>:8698`
   - 在外面 4G：手机装 Tailscale 开 VPN，填 `ws://<Mac 的 100.x 地址>:8698`
   - token 填服务端 `config.yaml` 的 `auth_token`。
5. 点「开始对话」授权麦克风即聊。

### 档 2：稳定自用（体验版）
开发者工具「上传」代码 → 后台「版本管理」设为体验版 → 添加体验成员。
体验版同样需要手机开「开发调试」跳过域名校验；不想开调试就得走档 3 的域名，或微信云托管
`wx.cloud.connectContainer`（免合法域名，但要在云容器里做 relay 回连 Mac，架构复杂，用得着再说）。

### 档 3：对外发布（真要给别人用才走）
- 等备案通过；
- 备案域名 + 公网服务器 443 端口 wss 反代回 Mac（家里无公网 IP，需 frp/中转）；
- 后台「开发管理-开发设置-服务器域名」配 socket 合法域名；
- 提审注意：对外提供 AI 生成内容涉及算法备案/内容审核等额外监管要求，自用不涉及。

## 服务端准备（Mac 侧）

`config.yaml` 需要（手机可达 + 鉴权门）：

```yaml
host: 0.0.0.0
auth_token: <一串随机字符>
```

改完重启 Hermes-Live.app（壳守护服务）。

## 真机待验证点（开发者工具/真机才能测，如实交代）

- `RecorderManager` PCM 实际帧长与回调频率（代码已做缓冲重切，理论无关紧要）；
- **iOS 上录音与 WebAudio 同时工作**：播音中开麦（barge-in）可能受 iOS 音频会话策略影响（音量 duck 或采集静音），不行就把打断降级为按钮；
- WebAudio 24000Hz buffer 在 iOS 的重采样音质。
