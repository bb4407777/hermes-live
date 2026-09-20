// 对话状态机与连接管理：对齐 web/app.js（setState/handleMessage/断线自愈/单飞防重入）。

import Foundation
import SwiftUI

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

    let serverAddr: String
    let serverToken: String

    private let ws = HermesWSClient()
    private let audio = AudioController()
    private var curTurn = -1
    private var agentBubbleId: UUID?
    private var agentTurn = -1
    private var reconnectTask: Task<Void, Never>?
    private var reconnectDelay: UInt64 = 1

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
            if !audio.running {
                AVAudioSession.sharedInstance().requestRecordPermission { [weak self] granted in
                    Task { @MainActor in
                        guard let self else { return }
                        if granted {
                            self.audio.start()
                            self.connectAndStart()
                        } else {
                            self.addLine(.error, "无麦克风权限，请到系统设置开启")
                            self.stopAll(status: "无麦克风权限")
                        }
                    }
                }
            } else {
                connectAndStart()
            }
        }
    }

    func interrupt() { ws.send(json: ["type": "interrupt"]) }

    /// 音色/语速改了即时同步（对齐 web pushConfig）
    func pushConfig() {
        guard ws.isOpen else { return }
        ws.send(json: ["type": "set_config", "voice": voice, "tts_rate": rate])
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
        comp.scheme = "http"
        let parts = serverAddr.split(separator: ":", maxSplits: 1).map(String.init)
        comp.host = parts.first ?? serverAddr
        if parts.count > 1 { comp.port = Int(parts[1]) }
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

    private func connectAndStart() {
        cancelReconnect()
        ws.connect(addr: serverAddr, token: serverToken)
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

    private func stopAll(status: String) {
        isLive = false
        cancelReconnect()
        ws.send(json: ["type": "stop"])
        ws.close()
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
            let voice = msg["voice"] as? String ?? ""
            let asr = msg["asr_model"] as? String ?? ""
            let note = msg["note"] as? String ?? ""
            statusText = "音色 \(voice) · ASR \(asr)" + (note.isEmpty ? "" : " · \(note)")
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
        audio.enqueue(turn: turn, pcm: data.subdata(in: 2...))
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
