// 对话状态机与连接管理：对齐 web/app.js（setState/handleMessage/断线自愈/单飞防重入）。

import Foundation
import SwiftUI
import AVFoundation

struct ChatMsg: Identifiable {
    enum Kind { case user, agent, tool, error }
    let id = UUID()
    let kind: Kind
    var text: String
}

@MainActor
final class ChatViewModel: ObservableObject {
    @Published var state = "idle"
    @Published var statusText = "未连接"
    @Published var transcript: [ChatMsg] = []
    @Published var partialText = ""
    @Published var isLive = false          // 对齐 app.js wantLive：用户意图“正在对话”
    @Published var orbLevel: Float = 0
    @Published var voice = "zh-CN-XiaoxiaoNeural"
    @Published var rate = "+30%"
    /// 输入形态：text=打字（默认，不收音）｜voice=说话（收音）
    /// 两种模式回答都出声播报。
    @Published var inputMode = "text"

    let serverAddr: String
    let serverToken: String

    private let ws = HermesWSClient()
    private let audio = AudioController()
    private var curTurn = -1
    private var agentBubbleId: UUID?
    private var agentTurn = -1
    private var reconnectTask: Task<Void, Never>?
    private var reconnectDelay: UInt64 = 1
    private var stoppedByBackground = false
    /// 服务端会话 id：切后台/断线后靠它回挂，别再新开一条
    private var sid: String = UserDefaults.standard.string(forKey: "hl_sid") ?? ""

    init(serverAddr: String, serverToken: String) {
        self.serverAddr = serverAddr
        self.serverToken = serverToken

        ws.onOpen = { [weak self] in
            Task { @MainActor in self?.didConnect() }
        }
        ws.onText = { [weak self] text in
            Task { @MainActor in self?.handleText(text) }
        }
        ws.onBinary = { [weak self] data in
            Task { @MainActor in self?.handleBinary(data) }
        }
        ws.onClose = { [weak self] code in
            Task { @MainActor in self?.handleClose(code) }
        }
        audio.onFrame = { [weak self] frame in
            self?.ws.send(audio: frame)
        }
        audio.onLevel = { [weak self] level in
            Task { @MainActor in self?.orbLevel = level }
        }
        audio.onPlaybackDone = { [weak self] turn in
            self?.ws.send(json: ["type": "playback_done", "turn": turn])
        }
    }

    // MARK: - 用户操作

    func toggle() {
        if isLive {
            stopAll(status: "已暂停")
        } else {
            isLive = true
            statusText = "连接中…"
            UIApplication.shared.isIdleTimerDisabled = true   // 对话中不锁屏
            if audio.running {
                _ = audio.setMicEnabled(inputMode == "voice")   // 形态可能在停止期间改过
                connectAndStart()
            } else {
                startAudio()
            }
        }
    }

    /// 按输入形态开音频：打字模式只开播放（不收音），语音模式才申请麦克风
    private func startAudio() {
        guard inputMode == "voice" else {
            if !audio.start(mic: false) { addLine(.error, "音频输出启动失败，回答可能没声音") }
            connectAndStart()
            return
        }
        AVAudioSession.sharedInstance().requestRecordPermission { [weak self] granted in
            Task { @MainActor in
                guard let self else { return }
                if granted {
                    if !self.audio.start(mic: true) { self.addLine(.error, "麦克风启动失败") }
                    self.connectAndStart()
                } else {
                    self.addLine(.error, "无麦克风权限，请到系统设置开启")
                    self.stopAll(status: "无麦克风权限")
                }
            }
        }
    }

    func interrupt() { ws.send(json: ["type": "interrupt"]) }

    /// 音色/语速改了即时同步（对齐 web pushConfig）
    func pushConfig() {
        guard ws.isOpen else { return }
        ws.send(json: ["type": "set_config", "voice": voice, "tts_rate": rate])
    }

    /// 切换输入形态：打字=停麦克风（不收音），语音=开麦克风接着听。回答始终出声。
    func setInputMode(_ mode: String) {
        guard mode != inputMode else { return }
        inputMode = mode
        statusText = mode == "voice" ? "语音输入，开口说话" : "打字输入，回车发送"
        if !isLive { return }          // 未开始：下次开始时按新形态起音频
        if mode == "voice" {
            AVAudioSession.sharedInstance().requestRecordPermission { [weak self] granted in
                Task { @MainActor in
                    guard let self else { return }
                    if granted {
                        if !self.audio.setMicEnabled(true) {
                            self.addLine(.error, "麦克风启动失败，稍后再试")
                            self.inputMode = "text"
                            self.statusText = "打字输入，回车发送"
                        }
                    } else {
                        self.addLine(.error, "无麦克风权限，请到系统设置开启")
                        self.inputMode = "text"
                        self.statusText = "打字输入，回车发送"
                        _ = self.audio.setMicEnabled(false)
                    }
                }
            }
        } else {
            _ = audio.setMicEnabled(false)
        }
    }

    func newSession() {
        ws.send(json: ["type": "new_session"])
        transcript.removeAll()
    }

    func sendText(_ text: String) {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        addBubble(.user, t)   // 文字轮不经 ASR，用户气泡在发送侧渲染
        if ws.isOpen {
            ws.send(json: ["type": "text", "text": t])
        } else {
            ws.connect(addr: serverAddr, token: serverToken)
            pendingText = t
        }
    }

    func restartServer() {
        addLine(.tool, "✓ 服务重启中，稍后自动重连…")
        var comp = URLComponents()
        if serverAddr.contains("://"), let u = URLComponents(string: serverAddr) {
            comp.scheme = (u.scheme == "https" || u.scheme == "wss") ? "https" : "http"
            comp.host = u.host
            comp.port = u.port
        } else {
            comp.scheme = "http"
            let parts = serverAddr.split(separator: ":", maxSplits: 1).map(String.init)
            comp.host = parts.first ?? serverAddr
            if parts.count > 1 { comp.port = Int(parts[1]) }
        }
        comp.path = "/api/restart"
        if !serverToken.isEmpty {
            comp.queryItems = [URLQueryItem(name: "token", value: serverToken)]
        }
        if let url = comp.url {
            var req = URLRequest(url: url)
            req.httpMethod = "POST"
            URLSession.shared.dataTask(with: req).resume()
        }
        ws.close()
        // 给服务 2 秒退出 + 2 秒启动（对齐网页版 4 秒重连）
        reconnectTask?.cancel()
        reconnectTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 4_000_000_000)
            guard !Task.isCancelled else { return }
            self?.connectAndStart()
        }
    }

    // MARK: - 连接管理（断线自愈：意图在就自动重连，退避 1s→10s）

    private var pendingText: String?

    /// 切后台：只停麦克风与播放，本轮生成继续跑（服务端文字留底，回来补齐）。
    func pauseForBackground() {
        guard isLive else { return }
        stoppedByBackground = true
        ws.send(json: ["type": "detach"])
        audio.stop()
        statusText = "已切后台，回答继续生成"
    }

    /// 回前台：连接还在就接回这一轮；掉了就带 sid 重连（服务端挂起池里还在）。
    func resumeAfterForeground() {
        if stoppedByBackground {
            stoppedByBackground = false
            if ws.isOpen {
                _ = audio.start(mic: inputMode == "voice")
                ws.send(json: ["type": "attach"])
                statusText = "已接回后台生成的回答"
            } else if !isLive {
                toggle()
            } else {
                statusText = "重连中…"
                connectAndStart()
            }
            return
        }
        guard isLive, !ws.isOpen else { return }
        statusText = "重连中…"
        connectAndStart()
    }

    private func connectAndStart() {
        cancelReconnect()
        ws.connect(addr: serverAddr, token: serverToken, sid: sid)
    }

    private func didConnect() {
        reconnectDelay = 1
        statusText = "已连接"
        ws.send(json: ["type": "set_config", "voice": voice, "tts_rate": rate])
        if let t = pendingText {
            pendingText = nil
            ws.send(json: ["type": "text", "text": t])
        }
        if isLive {
            ws.send(json: ["type": "start"])
        }
    }

    private func handleClose(_ code: Int?) {
        if code == 4001 {
            // 被新连接顶替（服务端单活跃连接）：退场，不与新端互踢
            addLine(.tool, "已在其他端打开，本端停止")
            stopAll(status: "已在其他端打开，本端停止")
            return
        }
        setState("idle")
        if isLive {
            scheduleReconnect()
        } else {
            statusText = "连接断开"
        }
    }

    private func scheduleReconnect() {
        guard isLive, reconnectTask == nil else { return }
        statusText = "连接断开，\(reconnectDelay)s 后重连…"
        let delay = reconnectDelay
        reconnectDelay = min(reconnectDelay * 2, 10)
        reconnectTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: delay * 1_000_000_000)
            guard let self, !Task.isCancelled else { return }
            self.reconnectTask = nil
            guard self.isLive else { return }
            self.ws.connect(addr: self.serverAddr, token: self.serverToken)
        }
    }

    private func cancelReconnect() {
        reconnectTask?.cancel()
        reconnectTask = nil
        reconnectDelay = 1
    }

    private func rememberSid(_ id: String) {
        sid = id
        UserDefaults.standard.set(id, forKey: "hl_sid")
    }

    private func stopAll(status: String) {
        isLive = false
        cancelReconnect()
        ws.send(json: ["type": "stop"])
        ws.close()
        rememberSid("")   // 主动结束＝这条会话用完，下次起新的
        audio.stop()
        setState("idle")
        statusText = status
        UIApplication.shared.isIdleTimerDisabled = false
    }

    // MARK: - 消息处理（对齐 app.js handleMessage）

    private func handleText(_ text: String) {
        guard let data = text.data(using: .utf8),
              let msg = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = msg["type"] as? String else { return }
        switch type {
        case "hello":
            rememberSid(msg["session_id"] as? String ?? "")
            let voice = msg["voice"] as? String ?? ""
            let asr = msg["asr_model"] as? String ?? ""
            let note = msg["note"] as? String ?? ""
            statusText = "音色 \(voice) · ASR \(asr)" + (note.isEmpty ? "" : " · \(note)")
        case "replay":
            // 切后台/断线回来：补齐挂起期间跑出来的这一轮（只补文字，不补播音频）
            rememberSid(msg["session_id"] as? String ?? "")
            if let turn = msg["turn"] as? Int { curTurn = turn }
            if let asr = msg["asr_text"] as? String, !asr.isEmpty { addBubble(.user, asr) }
            if let text = msg["text"] as? String, !text.isEmpty {
                let bubble = addBubble(.agent, text)
                agentBubbleId = (msg["done"] as? Bool == true) ? nil : bubble.id
                agentTurn = msg["turn"] as? Int ?? -1
            }
            setState(msg["state"] as? String ?? "listening", turn: msg["turn"] as? Int)
            statusText = (msg["done"] as? Bool == true) ? "已补回后台生成的回答" : "已接回后台正在生成的回答"
        case "state":
            setState(msg["state"] as? String ?? "idle", turn: msg["turn"] as? Int)
        case "asr_partial":
            partialText = msg["text"] as? String ?? ""
        case "asr_final":
            partialText = ""
            addBubble(.user, msg["text"] as? String ?? "")
        case "agent_delta":
            let turn = msg["turn"] as? Int ?? -1
            if agentTurn != turn || agentBubbleId == nil {
                let bubble = addBubble(.agent, "")
                agentBubbleId = bubble.id
                agentTurn = turn
            }
            if let id = agentBubbleId, let i = transcript.firstIndex(where: { $0.id == id }) {
                transcript[i].text += msg["text"] as? String ?? ""
            }
        case "agent_done":
            agentBubbleId = nil
        case "tool_progress":
            let emoji = msg["emoji"] as? String ?? "⚙️"
            let label = msg["label"] as? String ?? msg["tool"] as? String ?? "工具执行中"
            let done = msg["status"] as? String == "completed"
            addLine(.tool, "\(emoji) \(label)\(done ? " ✓" : "…")")
        case "tts_sentence":
            break   // 预留：逐句高亮
        case "tts_end":
            if let turn = msg["turn"] as? Int {
                audio.markTtsEnd(turn: turn)
            }
        case "replaced":
            // 服务端单活跃连接：本端被顶替，主动退场，不再自动重连
            addLine(.tool, "已在其他端打开，本端停止")
            stopAll(status: "已在其他端打开，本端停止")
        case "restarting":
            addLine(.tool, "✓ 服务重启中…")
        case "error":
            addLine(.error, msg["message"] as? String ?? "未知错误")
        default:
            break
        }
    }

    private func handleBinary(_ data: Data) {
        guard data.count > 2, data[0] == 0x01 else { return }
        let turn = Int(data[1])
        audio.enqueue(turn: turn, pcm: data.subdata(in: 2..<data.count))
    }

    private func setState(_ newState: String, turn: Int? = nil) {
        state = newState
        if newState != "listening" { partialText = "" }
        if let turn, turn != curTurn {
            curTurn = turn
            audio.setTurn(turn)   // 清播放缓冲：旧 turn 音频作废
        }
        // 半双工：speaking/thinking 时停发麦克风帧，listening 时恢复
        audio.micPaused = (newState == "speaking" || newState == "thinking")
        if newState != "listening" { orbLevel = 0 }
    }

    // MARK: - 气泡

    @discardableResult
    private func addBubble(_ kind: ChatMsg.Kind, _ text: String) -> ChatMsg {
        let msg = ChatMsg(kind: kind, text: text)
        transcript.append(msg)
        return msg
    }

    private func addLine(_ kind: ChatMsg.Kind, _ text: String) {
        transcript.append(ChatMsg(kind: kind, text: text))
    }
}
