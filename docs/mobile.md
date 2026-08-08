# 手机上用 Hermes-live 的三条路（按成本排序）

## 路线 1：零开发 —— Tailscale + 现有网页（推荐先走）

前端本来就只用标准 API（getUserMedia/WebSocket/AudioWorklet，iOS Safari 14.5+ 全支持），
不写一行新代码，手机在外面 4G 也能连回家里这台 Mac 语音聊 Hermes。

**关键卡点**：iOS Safari 的 getUserMedia 只认 HTTPS（127.0.0.1 例外仅限本机），
所以不能裸 IP 访问，必须走 `tailscale serve` 拿受信证书：

```bash
# Mac 侧（tailscale 当前是 stopped，先拨上线）
tailscale up
tailscale serve --bg --https=443 http://127.0.0.1:8698   # 自动签发 https 证书，只在 tailnet 内可达
tailscale serve status                                    # 拿到 https://<mac名>.<tailnet>.ts.net
```

iPhone：装 Tailscale app 登同一账号 → Safari 打开上面的 https 地址 →
分享 →「添加到主屏幕」= 全屏类 app 图标。

限制：锁屏/切后台 PWA 会被挂起（对讲式用法没问题，挂机长听不行）；
Mac 端 hermes-live 服务得在跑（菜单栏壳开着即可）。
关闭入口：`tailscale serve --https=443 off`。

## 路线 2：免费真 app（个人签名）

Xcode + 免费 Apple ID「个人团队」：把 macos/ 的壳思路移植成 iOS WKWebView 壳（约百行），
真机直装。代价：**签名 7 天过期**，每周要连 Xcode 重装一次，自用很烦。只有想要
原生麦克风后台权限/推送时才值得。

## 路线 3：99 美元/年开发者账号

签名一年有效、TestFlight 可分发给同事、可上锁屏常驻音频权限。等路线 1 用出瘾了再花这钱。

**结论**：先路线 1（十分钟能通），体验满意再谈 2/3。
