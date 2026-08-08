# 提案：给 api_server 平台配精简 toolset（压语音首字延迟）

**状态：配置已落（2026-08-08 12:0x，0.20 升级完成后），待下次网关重启生效**。
备份：`~/.qclaw-hermes/config.yaml.bak-20260808-toolsets`；已验 YAML 解析与结构完整；
`terminal/delegation` 两名在 0.20 网关的 weixin 段生产在用 = 名字合法性已证。
重启仍按红线归高律师择时会话外做；生效后跑 `scripts/ws_regression.py` 前后对比首 delta，
并把「api_server=精简面」补进 hermes SKILL.md（定调双写在生效时点做，不提前）。

## 背景

Hermes-live 语音对话的延迟大头是首 delta 3-4s，慢在 agent 大 prompt 预填充（模型已是 pool-deepseek-v4-flash，换模型无收益）。现状（`/Users/gao/.qclaw-hermes/config.yaml`）：

```yaml
platform_toolsets:
  weixin:
    - terminal
    - delegation
```

**微信通道早已用精简面**；api_server 没配 → 全量工具面 → prompt 显著更大。语音走的正是 api_server。

## 建议改法（方案 A，推荐）

```yaml
platform_toolsets:
  weixin:
    - terminal
    - delegation
  api_server:          # ← 新增两行，与 weixin 对齐
    - terminal
    - delegation
```

- 语义依据：0.19 源码 `agent/agent_init.py:1988` 一带（enabled_toolsets 按平台门控，空列表也合法）；0.20 树同样有该键（`hermes_cli/toolset_validation.py`）。
- 效果预期：api_server 的 prompt 面缩到与微信同级，首字预填充时间应明显下降（具体数值重启后用 `~/Code/hermes-live/scripts/ws_regression.py` 前后对比）。
- **波及面（要知情）**：8647 的其他 HTTP 调用方（cli/api 工具类会话）同样变成精简面。它们保留 terminal+delegation（分发能力不丢）；若日后有调用方需要全量工具，再上方案 B。

## 备选（方案 B，更外科但更重）

`multiplex_profiles` + 独立 voice profile（`/p/voice/v1/...` 路由，`api_server.py:1393-1455`），语音单独一套 config/toolset，默认 profile 分毫不动。代价：新建 profile 目录 + 开 multiplex + hermes-live 改 base_url。方案 A 不够用再上。

## 落地步骤（给执行者）

1. 备份 `config.yaml`；按方案 A 加两行。
2. 择时会话外重启网关（0.20 迁移若同窗进行，合并一次重启）。
3. 验证：`curl -s -H "Authorization: Bearer hermes-local-key" http://127.0.0.1:8647/v1/health`；跑 `~/Code/hermes-live/scripts/ws_regression.py`（2 次 LLM 调用）看首 delta 变化；微信通道随手发一条确认无恙。
