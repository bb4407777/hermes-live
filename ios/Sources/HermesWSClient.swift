// WebSocket 客户端：对齐 server/protocol.py + server/session.py。
// 二进制帧：上行 0x01+PCM16(16k)；下行 0x01+turn+PCM16(24k)。控制走 JSON 文本帧。
// URLSessionWebSocketTask 自动应答服务端 ping（aiohttp heartbeat=30），无需手动保活。

import Foundation

final class HermesWSClient: NSObject {
    var onOpen: () -> Void = {}
    var onText: (String) -> Void = { _ in }
    var onBinary: (Data) -> Void = { _ in }
    /// code 4001 = 被新连接顶替（对齐 web 端处理）；nil = 异常断开/失败
    var onClose: (Int?) -> Void = { _ in }

    private var session: URLSession?
    private var task: URLSessionWebSocketTask?
    private(set) var isOpen = false

    func connect(addr: String, token: String) {
        close()
        var comp = URLComponents()
        comp.scheme = "ws"
        // addr 形如 "192.168.1.5:8698" 或 "100.x.x.x:8698"
        let parts = addr.split(separator: ":", maxSplits: 1).map(String.init)
        comp.host = parts.first ?? addr
        if parts.count > 1 { comp.port = Int(parts[1]) }
        comp.path = "/ws"
        if !token.isEmpty {
            comp.queryItems = [URLQueryItem(name: "token", value: token)]
        }
        guard let url = comp.url else {
            onClose(nil)
            return
        }
        let s = URLSession(configuration: .default, delegate: self, delegateQueue: nil)
        session = s
        let t = s.webSocketTask(with: url)
        t.maximumMessageSize = 4 * 1024 * 1024   // 对齐服务端 max_msg_size
        task = t
        t.resume()
        // webSocketTask 没有 onopen 回调：resume 后 ping 通即视为已连接
        t.sendPing { [weak self] error in
            guard let self else { return }
            if error != nil {
                self.fail()
            } else if !self.isOpen {
                self.isOpen = true
                self.onOpen()
            }
        }
        receive()
    }

    func send(json obj: [String: Any]) {
        guard isOpen, let data = try? JSONSerialization.data(withJSONObject: obj),
              let str = String(data: data, encoding: .utf8) else { return }
        task?.send(.string(str)) { _ in }
    }

    func send(audio pcm: Data) {
        guard isOpen else { return }
        var frame = Data(count: 1 + pcm.count)
        frame[0] = 0x01
        frame.replaceSubrange(1..., with: pcm)
        task?.send(.data(frame)) { _ in }
    }

    func close() {
        isOpen = false
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        session?.invalidateAndCancel()
        session = nil
    }

    private func fail() {
        guard isOpen || task != nil else { return }
        isOpen = false
        task = nil
        session?.invalidateAndCancel()
        session = nil
        onClose(nil)
    }

    private func receive() {
        task?.receive { [weak self] result in
            guard let self, self.task != nil else { return }
            switch result {
            case .success(let msg):
                switch msg {
                case .string(let s): self.onText(s)
                case .data(let d): self.onBinary(d)
                @unknown default: break
                }
                self.receive()
            case .failure:
                self.fail()
            }
        }
    }
}

extension HermesWSClient: URLSessionWebSocketDelegate {
    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode,
                    reason: Data?) {
        let wasOpen = isOpen
        isOpen = false
        task = nil
        if wasOpen {
            onClose(closeCode == .invalid ? nil : closeCode.rawValue)
        }
    }
}
