# iOS 版安装（免费个人签，7 天有效期）

> 2026-08-14 起为**原生音频版**：SwiftUI + AVAudioEngine 录音/播放，WebSocket 直连 Mac，
> 不再走 WKWebView 托管网页。带了 `UIBackgroundModes=audio`，**锁屏/切后台对话不断**
> （旧 WebView 壳锁屏即挂起）。安装步骤不变。

## 一次性准备（约 30 分钟，大头是下载 Xcode）

1. **装 Xcode**：App Store 搜 Xcode 安装（免费，约 15GB；本机目前只有命令行工具，编不了 iOS）。
2. **登 Apple ID**：Xcode → Settings → Accounts → ➕ 添加你的 Apple ID（免费账号即可，自动生成 Personal Team）。
3. **iPhone 开开发者模式**：设置 → 隐私与安全性 → 开发者模式 → 开，重启手机。

## 装到手机（之后每 7 天重复一次，2 分钟）

1. 打开 `~/Code/hermes-live/ios/HermesLive.xcodeproj`（若不存在先 `cd ios && xcodegen generate`）。
2. 选中 target HermesLive → Signing & Capabilities → Team 选你的 Personal Team
   （bundle id 撞车就在后面加几个字母）。
3. iPhone 插线（首次点「信任此电脑」），顶部设备选你的 iPhone → ⌘R。
4. 首次运行手机会拦：设置 → 通用 → VPN 与设备管理 → 信任你的开发者证书。

## Mac 侧配套（一次性）

手机要连进来，Mac 的服务不能只听回环。`~/Code/hermes-live/config.yaml`（没有就从 example 复制）加：

```yaml
host: 0.0.0.0
auth_token: 随便一串长密码     # 暴露到局域网必须设，app 设置页填同一串
```

重启服务：`curl -X POST http://127.0.0.1:8698/api/restart`，或 launchd 常驻的话
`kill $(pgrep -f server.main)`（KeepAlive 会自动拉起）。

## 手机端使用

- 首屏填 Mac 地址：家里 Wi-Fi 填局域网 IP（Mac 上 `ipconfig getifaddr en0` 查）+ `:8698`；
  两端都装 Tailscale 的话填 Mac 的 100.x 地址，**在外面 4G 也能连**（且免局域网权限弹窗）。
- Token 填 Mac 配置里那串。右上角齿轮可改。
- 打开即自动连接并开始对话；点圆球停止/开始。
- 首次会弹两个授权：本地网络（连局域网 Mac 需要）+ 麦克风。
- **锁屏/切后台继续对话**：音频后台模式保活，断线会自动重连（退避 1s→10s）。

## 已知边界

- 个人签 **7 天过期**：app 打不开时连 Xcode ⌘R 重装即可，数据（服务器地址）不丢。
- 接电话等音频打断后会自动恢复引擎；若收音异常，点圆球停一次再开即可。
- 架构与网页版同协议（WS 二进制帧 + JSON 控制），`server/` 改动两端自动生效；
  `web/` 页面改动不再影响 iOS 端（原生 UI 在 `ios/Sources/ChatView.swift`）。
