# Changelog

## [0.4.8] - 2026-09-20

> 0.4.7 重启后高律师手机网页端「没有声音」的回归修复。方向与一开始的猜测相反：
> 不是隧道、不是 ASR（他的三句话都被正确识别）、也不是 edge-tts（日志零报错）、
> 不是服务端下发时序（`state` 确实在首帧音频之前）——最可能的机制是**前端几个 JS 被分别缓存成了新旧混合**
> （离线能完整复现并修掉，设备侧还没等他复听确认，见末节）。
> 顺带对整条下行链路做了一次二次排查，另出三处确认的真 bug（下 5、6、7 条）。

### Fixed
- **网页端整场静音（0.4.7 零拷贝改造引入的错配窗口）**：0.4.7 把下行块从「预切掉 2 字节头的独立缓冲」
  改成「整块 ArrayBuffer + `offset`」，worklet 于是依赖 `d.offset`。而 `app.js` 与 `player-processor.js`
  是两次独立请求，静态资源只有 ETag 没有 `Cache-Control`，Safari 按「距 Last-Modified 约 10%」各自启发式
  新鲜度 → 重启后刷新，手机可能拿到**旧 app.js（不发 offset）+ 新 worklet**：`undefined` 让长度算成
  `NaN >> 1 === 0`，每一块都解成 0 个样本 → **全程无声，而麦克风/ASR/状态机一切正常**，页面看不出任何异常。
  现 worklet 侧 `const off = d.offset | 0`（新旧两种调用形态都出声），并加 `n <= 0` 早返回
- **同一 turn 重复 `setTurn` 把已排队音频啃成碎片**：`setTurn` 原先无条件清缓冲，而主线程的 `state` 事件
  与 `clear()` 都会发它。现同 turn 幂等（`if (t !== this.turn)` 才清），真要丢缓冲走新增的 `clear` 消息；
  `AudioIO.clear()` 相应改发 `{type:'clear'}`（不能再借道 setTurn：`_turn` 初始值 -1 经 `& 0xFF` 会变成 255，
  反而把 worklet 的 turn 弄错）
- **前端资源强制回源校验**：`server/main.py` 新增 `no_cache_assets` 中间件，对 `/` 与 `/web/*`
  加 `Cache-Control: no-cache`。实测本地起 app：三个 JS 均回 `no-cache`，带旧 ETag 回源得 304（几百字节，
  代价可忽略）。⚠️ **此项需重启方生效**，尚未重启
- **`playback_done` 服务端终于可见**：`session.py` 原先收到回报时一行日志都不打，于是「客户端到底播没播」
  在这次事故里完全无法回答——服务端看着一切正常。现在收到就记 `turn`（无在途 turn 时标注忽略）
- **合成中途死掉就没有 `tts_end`（服务端，`_tts_consumer`）**：`tts_end` 只在 happy path 发，
  而 edge-tts 重试后仍失败时异常从 `await inflight` 逃出，半段音频已经下发、收口信号却没发。
  客户端的 `ended` 门只认 `tts_end`：门不开 → 尾音永不上报播完，而服务端已按异常路径回 `listening`
  并放开麦克风 → **AI 把自己念的尾巴录进下一轮**。现把补发挪进 `finally`
  （实测事件序：`bin bin tts_sentence tts_end error state:listening`）
- **小程序端同一个 `setTurn` 坑还活着（`miniprogram/utils/audio.js:97`）**：服务端每个话轮发三条
  **同 turn** 的 `state`（thinking / speaking / listening），小程序的 `setTurn` 无条件 `stop()` 掉全部
  已排度音源并复位 `_ended`/`_doneTimer`。前两条在音频之前、无害，但超时与异常路径上那条 `listening`
  是在播放中途到达的 → **当场掐断尾音且 `playback_done` 再也不上报**。现同 turn 直接返回，
  与 web worklet 对齐；这条路径 web 端靠「主线程只在 turn 变化时才调 setTurn」侥幸躲过，两套实现
  从此各有一条仿真断言守着
- **半开隧道期间为死连接白烧网关 token（`session.py:on_audio`）**：`on_conn_lost()` 只把 `closed`
  置真、掐掉出站，`main.py` 的 `async for msg in ws` 读侧却照常收麦克风帧 → 照常 VAD → 照常起话轮
  → 照常打网关出答案，只是没有一个字节能送到手机上；要等 heartbeat（30s）把连接判死才停，
  这期间他每说一句就是一轮完整推理。现 `on_audio` 见 `closed` 直接丢帧

### 验证与仍未证实的部分
- `node tests/worklet_sim.mjs` **20 项全绿**（本轮新增 4 项：无 offset 也出声、重复 setTurn 不啃缓冲、
  `clear` 丢缓冲但保住 turn、小程序同 turn `setTurn` 不掐尾音）。`pytest` **43 项全绿**
  （新增 2 项协议回归：合成死掉仍有 `tts_end`、`closed` 后不再跑 VAD）
- **新写的每条断言都先证明它会红**：把对应修复删掉后重跑——`tts_end` 补发移除 → 该测超时失败；
  `on_audio` 短路移除 → 该测报"closed 之后还在跑 VAD"；小程序 `setTurn` 守卫移除 → 20 项里恰好只红
  它自己那一条。变异表也补了一条常驻变异（同 turn `setTurn` 仍清缓冲），并修掉因本次改写而失配的
  老锚点（`this.turn = d.turn & 0xFF;` → `const t = d.turn & 0xFF;`）
- **小程序 `Player` 之前完全没有任何自动化覆盖**（只有 web worklet 有），所以它的 `setTurn` 坑一直没被发现；
  现在用假 WebAudio 上下文 + 假时钟把它接进了同一个仿真脚本，`HL_MP_AUDIO` 可指向副本做变异
- **排查过程里我错过两次，记在这里以免下次照抄**：① 第一版浏览器夹具的 `analyze()` 阈值和过零计数都是坏的，
  报出「100% 有声 / 0 Hz / peak 1.0」这种自相矛盾的数字，直到用已知 220Hz 正弦与「从不写输出的 worklet」
  两个对照标校后才对上（962ms / 220.6Hz / peak 0.244 / ratio 0.5000）；② 我一度据此认定「瞬时断流后
  `queued` 该按队列真实剩余重算」并改了代码，实际 `_next()` 只在 `chunks` 为空时返回 null，那个分支不可达，
  已回退。真正在设备上跑一遍确认的仍是高律师本人——**手机端复听未回话之前，这条不能算结案**

## [0.4.7] - 2026-09-19

> 深度排查 + 卡顿修复。本节同时归档原先一直记在 `[Unreleased]` 下、已经提交但没打标的改动
> （qwen ASR sidecar、launchd 常驻、断连三连修），统一随 0.4.7 生效。
> ✅ **已上线**：2026-09-20 15:16 走 `scripts/restart-service.sh`（launchd `kickstart -k`）重启，
> 新进程 PID 96226 单实例、旧进程退出码 0，`/api/health` 回 `version: 0.4.7` + `auth_required: true`
> 且不再回显 `session_id`；`scripts/ws_regression.py` 9/9 通过。
> 网关侧坐实串历史已断：三条新会话全为 `hl-<uuid16>`、`history=0`、prompt 回到 **11,78x token**
> （修复前同场景 42,771）。真人试听仍待高律师手机确认（见已知问题 3）

### Security
- **访问日志不再落明文 token**：`logs/service.log` 此前把 `?token=…` 整条写进磁盘。新增 `TokenMaskingAccessLogger`，包住父类编译出的每个取值函数（`_format_*` 是绑到 `AccessLogger` 上的 staticmethod，子类覆写不生效；而 `AccessLogger.log()` 整体包在 try/except 里，取值函数抛异常等于这行日志被静默丢弃——`%s`/`%b` 返回 int，已加 `isinstance` 守卫）。实测重启后日志落 `token=***`
- **`/api/health` 不再回显 `session_id`**（远程可探测到网关会话身份），改回 `version` + `auth_required`；网页端据 `auth_required` 在未带 token 时给出准确提示，而不是笼统报"连不上"
- **token 比较改用 `hmac.compare_digest`**（原来 `!=` 走的是短路比较）
- ⚠️ **遗留（需人工收口）**：本次只是"以后不再写明文"，**已落盘的 `logs/service.log` 里那串 token 还在**；
  而且它曾随仓库提交进 git 历史（`eef9761` 0.3.2、`44f7a4e` 0.4.5 内置为小程序默认值，`40b139b` 才移除）。
  唯一干净的收尾是**轮换 `auth_token`** 并清旧日志——详见 PROJECT.md 已知问题 7

### Fixed
- **跨案上下文串用（本次最严重）**：语音链路从不带 `X-Hermes-Session-Id`，网关在缺头时按 `sha256(system_prompt + 首句)` 派生会话身份并从 `state.db` 回灌历史。语音的 system prompt 恒定、开场白高度重复（"在吗""看下这个"），不同案件因此被并进同一条网关会话——实测两次独立会话 `session_id` 完全相同，上下文从 11,801 token 涨到 42,771。现每条 WS 连接 `mint_session_id()` 生成 `hl-<uuid16>`，`headers()` 无条件携带（一旦为空就退回指纹派生，这个约束写进了 docstring），`new_session` 控制消息轮换身份并 `turn += 1` 同时清空回声缓冲
- **慢生成时提前收麦，把自己的 TTS 录进下一轮**：`playback_done` 等待窗按 `first_send + 音频总时长` 估算，而音频是边生成边下发的——模型想 40s、只出 20s 语音时该式大幅低估，喇叭还在播就打回 listening。现取 `max(first_send + dur, last_send)` 两个下界的较大者再加 `playback_grace_ms` 余量。写这条时先踩了个坑：`last_send` 只声明没赋值，测试断言到的等待窗刚好卡在 `max(0.5, …)` 下限，正是它证明了旧公式失效
- **网页端语音卡顿**：
  - TTS 预合成深度 `tts_lookahead` 1→2，句间不再等合成而断流
  - 播放 Worklet 加 120ms 预灌水位（`PREFILL_MS`），欠载后重新预灌而非立刻起播；chunk 边界做交叉插值，消除拼接爆音
  - 新增 `tts_end` 事件：客户端只在收到 `tts_end` 且缓冲排空后才上报 `playback_done`。此前靠 300ms 欠载猜测，句间停顿 >800ms 就被误判为播完 → 提前开麦 → 回声与中途截断
  - 录音 Worklet 改零分配（常驻 scratch + `copyWithin` 搬移，`postMessage` 转移 ArrayBuffer），消除 GC 抖动导致的上行断续；溢出时复位而不是无限堆
  - 上行背压：`ws.bufferedAmount > 64KB` 时丢帧，不再让弱网下的录音队列吃满内存
- **录音 worklet 首个渲染量子就抛 RangeError（改写引入的回归，仿真抓的）**：零分配改造用 `copyWithin` 搬残留，
  但 `pos` 每量子会越过缓冲尾最多 ratio 个样本（48k 时 3），`keep = pos|0` 便可能 > `n` →
  `carryLen` 变负 → 下一量子 `buf.set(ch, -1)` 直接 RangeError，音频线程一抛异常麦克风就彻底哑
  （旧写法 `buf.slice(keep)` 越界只会返回空数组，所以当年不炸）。现 `keep = Math.min(pos|0, n-1)`，
  既保住最后一个样本当右端点，也不像旧写法那样丢一个样本
- **第 256 轮起 AI 全程静音（协议级潜伏 bug）**：下行音频帧头只有 1 字节 turn（`0x01 + turn(u8)`），
  而 `state`/`tts_end` 事件带的是完整整数。两端都比 `帧头 == JSON`，从 256 起永不相等 →
  所有音频帧被当成"旧 turn 的迟到帧"丢掉，页面还在转圈但一句不听。
  要同一条连接撑满 256 轮才触发（刷新页面就归零），所以一直没露头。现网页 worklet 与小程序 Player
  都按 `& 0xFF` 比对，回报 `playback_done` 仍用完整 turn（服务端按它记 Event），
  约束写进 `server/protocol.py` 文档头
- **收口上报被预灌门冻住（我自己那条改动的二次 bug）**：加了 `PREFILL_MS` 水位后，结尾最后一个量子把
  `started` 清零，下一个量子就在门口早返回，`silentCalls` 冻在 1 → 300ms 欠载永远熬不满 →
  `playback_done` 再也不上报，服务端只能走超时兜底。现欠载计数抽成 `_idle()`，预灌等待期同样累计
- **批量 ASR 兜底绕开本机权重去下载 3 GB**：`ASR.load()` 只在 `asr_backend in ("auto","whispercpp")`
  时才试 whisper.cpp，于是线上钉 `doubao` 时一旦落到批量档（豆包会话失败、或跑 `cli.m0_pipeline --wav`）
  就直接 `_load_faster_whisper()`。而 `asr_model: large-v3` 走 hub 缓存布局时快照里只有 config/tokenizer、
  **没有 model.bin**（`models/faster-whisper-large-v3/` 那份 2.9 GB 完整目录因为路径布局不同没被命中），
  于是静默从 hf-mirror 拉 3 GB、卡在 xethub 上 7 分钟无进度（冒烟 4/4 就是这么挂的）。现：
  ① 除显式 `faster` 外一律先试本地 ggml 权重；② `download_root` 下存在完整
  `faster-whisper-<name>/model.bin` 时按「本地目录」传参（`WhisperModel` 见 isdir 即不走 hub）。
  `scripts/smoke.sh` 尾行 `echo失败`（少了空格，被当成命令名）同时修掉
- **ASR 档位回显骗人**：三条 `hello` 事件的 `asr_model` 取的是 `cfg.asr_model`，而那只是
  faster-whisper 的档位名——线上实配豆包时网页状态条仍显示 "ASR large-v3"（`web/app.js:103`），
  批量兜底真跑 whisper.cpp 时也是 "large-v3"。现统一走新的 `Session.asr_label()`（doubao /
  sherpa-paraformer / qwen3-asr-1.7b / `whisper.cpp:<权重文件名>` / `faster-whisper:<档位>`，
  取用序与 `_run_turn` 一致）；`/api/health` 本身已有探测链，只有落到批量那一支时同样会谎报，
  该支改读 `ASR.backend_label`
- **`ws_writer` 一次发送异常就整体退出**：出站队列此后只堆不发，页面状态永远卡在 thinking/speaking、音频静默（表现为"没反应但球还在转"）。现逐条 send 各自 try/except，失败即回调 `session.on_conn_lost()` 把 Session 打回 idle，写线程随后退出
- **前端连接竞态**：`connect()` 改为持有局部 `socket` 并用 `stale()` 守卫 `onopen/onerror/onclose/onmessage`，旧连接的回调不再污染新连接；重连前先 `audio.clear()` 并恢复播放（iOS 后台挂起的 AudioContext 在 `visibilitychange` 时 `resumeIfNeeded()`）
- **小程序**：`_sockSeq` + `_connectPromise` 单飞防重入；`_closeSocket()` 统一收口（改设置、`onUnload` 都真正关闭并复位 `connected/canInterrupt`，此前离开页面 socket 还挂着）；`endTurn(turn)` 加 turn 守卫，打断后旧 turn 的尾音不再挂住新 turn；说话中补「打断」按钮（`canInterrupt` 覆盖 thinking + speaking，此前长答案一旦开口就没有任何中断入口）
- **断连根因三连修（2026-08-11，网页版"只收音不识别"）**：
  - **doubao ASR 改按连接实例化 + 会话闭包隔离**：此前是 app 级单例被所有 WS 连接的 Session 共享，手机端双连接（autostart 与手点竞态/旧标签残留）互相 reset/覆盖对方会话 → 空结果四连、上轮文本窜入下轮（曾实测"停口→ASR 0.00s"）；0.4.4 期间记录的"session 每次立即结束 result=''"即此根因，当时误判为豆包侧问题
  - **豆包会话懒建连**：原在进入 listening 就建连，空闲 ~40s 被豆包远端掐流（`GrpcError: the stream is done`），此后整轮丢话；现 VAD start 事件才建连并补喂 pre-roll，feed 检测后台已死不再喂盲队列（PTT 模式保持进听即建连）
  - **服务端单活跃连接**：新 WS 顶替旧连接（发应用层 `replaced`，不跨协程 close——那样客户端收 1006 分不清被顶替还是断线）；旧 Session 打回 idle，僵尸连接由 heartbeat 兜底清理
  - **前端断线自愈**：connect 单飞防重入（双连接源头）；`wantLive` 意图标志 + 指数退避自动重连 + 重连后自动恢复 listening；息屏/切后台回前台（visibilitychange）立即补连；收到 `replaced` 主动退场不互踢。此前断线只改状态文字，麦克风还亮着但连接已死，即"只收音、不识别、不转指令"的直接体感
- **语音 prompt 增加同音字容错**：提示 LLM 按律师语境纠正 ASR 同音字误差（如"转锁"→"转所"）
- **asr_backend 分流错误**：`whispercpp`/`faster` 档此前会被 `!= "doubao"` 分支拦截、实际仍加载 sherpa；现仅 `auto`/`sherpa 才走 sherpa 流式，批量 ASR 配置真正生效
- **`server/asr.py` 文档头与实况脱节（读代码时被误导的就是它）**：整段没提 qwen sidecar（`f52468a` 已接进 `main.py` 探测链与 `Session._run_turn`），把批量 `ASR` 说成「仅 --text CLI 路径或 sherpa 不可用时使用」，`load()`/`_load_faster_whisper()`/类注释同错。现按实况改写：启动探测链 qwen > doubao > sherpa > 批量（`main.py`）、话轮取用序 doubao > sherpa > qwen > 批量（`session.py`），并注明 `cli.m0_pipeline --wav/--mic` 只传批量 ASR、不传流式后端
- **doubao ASR 空结果**：实测 session 每次立即结束（result=''），暂弃用，配置切回 faster-whisper（→ 根因已定位并修复，见上）

### Changed
- **版本号单一来源**：`server/__init__.py` 此前是**空文件**（`main.py` 里 `from . import __version__` 实际取不到东西），现定义 `__version__ = "0.4.7"`，启动日志与 `/api/health` 同源回显；PROJECT.md / README.md 从停留在 0.4.4 的过期记载同步到实况（协议、状态机、配置项、ASR 四档、kokoro TTS、launchd 运维）
- **半双工写进代码注释与文档**：`session.py` 的模块 docstring 此前描述的是已废弃的全双工 + barge-in，现改写为实际的半双工收口流程；PROJECT.md 里 `barge_*` 配置标注为 inert（保留代码但当前无入口触发），避免下次再按全双工排查
- **音频下行改 24kHz、`tts_end` 显式收口**：协议文档与 `web/`、`miniprogram/` 两端实现对齐，客户端不再靠欠载推断"本 turn 说完"
- **launchd 常驻**（高律师 2026-08-11 定）：`com.gaochengbin.hermes-live` KeepAlive 常驻，Mac 重启/进程崩溃自动拉起（实测 kill -9 后 ~10s 复活）；plist 模板入仓 `scripts/launchd/`；`restart-service.sh` 自动识别 launchd 走 `kickstart -k`（否则 KeepAlive 拉起 + 脚本 nohup 再起一个会双进程），网页/小程序重启按钮同路径，实测重启后单进程
- **尾静音 800→1100ms**（高律师 2026-08-11 定）：800ms 时思考停顿（"那个…"）被切碎单独成轮，调回 1100 换少切碎，每轮多等 0.3s
- **半双工维持不改**（高律师 2026-08-11 定）：thinking/speaking 期丢帧不做排队改造——经常出 bug
- **恢复流式朗读**：SSE 边生成边分句送 TTS，回退 0.4.3 的「等全量返回再统一入队」（保留禁 barge-in）；实测首句 3.74s / 首音 5.0s / 整轮播完 37.06s（高律师 2026-08-10 定）
- **ASR 切回原始 large-v3，最终落 whisper.cpp Metal**：`asr_backend: whispercpp` + `models/ggml/ggml-large-v3-q5_0.bin`（ModelScope `timeless/whispercpp` 下载，HF xet CDN 本机直连超时）；实测 RTF 0.47（faster-whisper CPU int8 为 1.37，约 3 倍提速）
- faster-whisper large-v3 权重（`models/faster-whisper-large-v3`，ModelScope `keepitsimple/faster-whisper-large-v3`）保留作兜底
- **替换应用图标**：使用 `~/Downloads/9154.png`（640×640 RGBA 黑白漫画风人像）作主图，`scripts/gen_icons.py` 一键重出 macOS iconset（10 档 RGBA）+ iOS 1024（白底 RGB，无 alpha 满足 App Store）+ xcassets 三处源 + 网页 favicon（`web/favicon.png`，`index.html` 挂 `<link rel="icon">`）；旧 `assets/icon-source.jpg` 进回收站
- **token 不再硬编码入库**：`miniprogram/app.js` 移除默认内置 auth_token，只从 `wx.getStorageSync('hl_token')` 读取；服务端 token 仍仅存 gitignored `config.yaml`（2026-08-11 定：禁随仓库外泄；老用户手机存储已缓存，不受影响）

### Added
- **ASR 切换 qwen3-asr 本地 sidecar**（2026-09-04 高律师定）：`asr_backend: qwen`——复用 weSaw 的 `scripts/qwenasr_server.py` + 专用 venv `.venv-qwenasr` + 权重 `~/.cache/models/Qwen3-ASR-1.7B`（4.4GB，MPS），新增 `server/qwen_asr.py` 常驻 sidecar 客户端（stdin/stdout JSON 行协议；服务启动后台预热，实测 ~16s ready；崩溃惰性重启；服务退出关 stdin 由 sidecar 读 EOF 自杀，不留孤儿；`asr_qwen_idle_kill_min` 可选空闲回收）。实测 4.5s 语音热转写 1.31s（RTF≈0.29，快于 whisper.cpp 0.47），断句标点更好，离线免豆包逆向依赖。**代价：批量模型无 asr_partial 实时字幕**，asr_final 一次到位；`auto` 档现在也优先 qwen（doubao 退居次选）

### Tests
- 新增 `tests/test_fixes_20260919.py`（14）：会话 id 唯一性 / `headers()` 必带 / `ws_writer` 异常路径 / 访问日志遮罩（含 `request=None` 与 int 返回值）/ `_check_token` 的 Bearer、错误 token、隧道强制三类
- 新增 `tests/test_session_protocol.py`（5）：用假 Hermes + 假 TTS 驱动**真实** `Session`，按时间戳断言事件序（`speaking` 早于首帧、`tts_end` 晚于末帧、末帧后至少等够播放时间）、打断后 turn 递增且不再产音、`new_session` 轮换身份
- 新增 `tests/worklet_sim.mjs`（14 条）：假 `AudioWorkletProcessor` + 按渲染量子推进，测两个音频 worklet
  （预灌水位、`end` 才收口、turn 取模、跨块 NaN、上行帧长/倍率/削波/电平节流）——
  上面「录音 worklet 首量子就抛」「第 256 轮静音」「收口被预灌门冻住」三条都是它先复现的
- 新增 `tests/test_worklet_mutants.py`（10 条变异检查）：把 worklet 逐条改坏，断言仿真必须变红，
  防止仿真自己退化成「永远全绿」的摆设
- 新增 `tests/test_fixes_20260919.py` 三条批量 ASR 用例：钉住「任何 `asr_backend` 档位都先试
  本机 ggml 权重」「`download_root` 下有完整 faster-whisper 快照就按本地目录加载」
  「回显给客户端的档位 = 真会用的那一档」。前两条做过变异检查（把改动退回旧写法必须变红）
- 冒烟 4/4 通过（2026-09-20 01:04，全程 36s；修复前第 4 步卡在 3 GB 权重下载不出来）
- `.venv/bin/python -m pytest tests/ -q` → 39 passed（node 缺失时 worklet 两条自动 skip）

## [0.4.6] - 2026-08-10

### Security
- **隧道流量强制 token 校验**：cloudflared 经本机回环转发，此前被 `_check_token` 回环豁免整体放行，`live.gaochengbin.com` 实际无密码暴露公网；现对带 `Cf-Ray` 头的请求强制 token（`?token=` 与 `Authorization: Bearer` 均认），公网无 token 实测 401

### Fixed
- `_check_token` 接受 Bearer 头（此前只认 query，重启按钮在 LAN 下会 401）

## [0.4.5] - 2026-08-10

### Changed
- **token 内置为小程序默认值**：沿用 0.3.2 默认 token 思路，首次使用免手填；服务端 `auth_token` 保持不变；设置弹窗只剩服务器地址一项（依据：体验版仅本人账户可登，高律师确认）

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
