# Cloudflare Tunnel：wss://live.gaochengbin.com

隧道把 `https/wss://live.gaochengbin.com` 路由到本机 `http://127.0.0.1:8698`，
小程序无需局域网即可连上服务。

## 组件
- 隧道 ID: `f7cf0ab6-af9d-4556-9e03-ed01bd289aea`
- 配置: `~/.cloudflared/hermes-live.yml`
- 凭据: `~/.cloudflared/f7cf0ab6-....json`（gitignored，勿丢）
- launchd plist: `~/Library/LaunchAgents/com.gaochengbin.hermes-tunnel.plist`（开机自启）

## 日常维护
```bash
# 查进程
pgrep -fl cloudflared

# 手动重启（launchd 一般自动拉起）
launchctl kickstart -k gui/$(id -u)/com.gaochengbin.hermes-tunnel

# 看日志
tail -f ~/.cloudflared/hermes-tunnel.log
```

## 小程序合法域名（需后台配置一次，扫管理员二维码）
后台：开发管理 → 开发设置 → 服务器域名 → socket 合法域名
值：`wss://live.gaochengbin.com`
