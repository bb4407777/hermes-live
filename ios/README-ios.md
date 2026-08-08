# iOS 版安装（免费个人签，7 天有效期）

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

重启 Hermes-Live 菜单栏 app（或 `kill $(cat tmp/server.pid)` 后重开）。

## 手机端使用

- 首屏填 Mac 地址：家里 Wi-Fi 填局域网 IP（Mac 上 `ipconfig getifaddr en0` 查）+ `:8698`；
  两端都装 Tailscale 的话填 Mac 的 100.x 地址，**在外面 4G 也能连**（且免局域网权限弹窗）。
- Token 填 Mac 配置里那串。右上角齿轮可改。
- 首次会弹两个授权：本地网络（连局域网 Mac 需要）+ 麦克风。

## 已知边界

- 个人签 **7 天过期**：app 打不开时连 Xcode ⌘R 重装即可，数据（服务器地址）不丢。
- 锁屏/切后台会挂起对话（iOS 对 WKWebView 的限制），回前台重新点开始即可。
- 页面由 app 内回环服务器托管（保 getUserMedia 的安全上下文），WebSocket 直连 Mac——
  架构与 Mac 版同源，`web/` 改一处两端生效（工程里是文件夹引用，不用拷贝）。
