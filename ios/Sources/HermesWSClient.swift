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

    func connect(addr: String, token: String, sid: String = "") {
        close()
        var comp = URLComponents()
        var urlToken: String?
        // 支持三种写法：整条链接（https://live.gaochengbin.com/?token=xxx，自动换 wss）、
        // 域名 live.gaochengbin.com（wss 443 公网隧道）、
        // 192.168.1.5:8698 / 100.x.x.x:8698（局域网直连 ws）
        if addr.contains("://") {
            guard let u = URLComponents(string: addr) else {
                onClose(nil)
                return
            }
            comp.scheme = (u.scheme == "https" || u.scheme == "wss") ? "wss" : "ws"
            comp.host = u.host
            comp.port = u.port
            let p = u.path
            if !p.isEmpty, p != "/" { comp.path = p }
            urlToken = u.queryItems?.first { $0.name == "token" }?.value
        } else {
            let parts = addr.split(separator: ":", maxSplits: 1).map(String.init)
            comp.host = parts.first ?? addr
            if parts.count > 1 {
                comp.scheme = "ws"
                comp.port = Int(parts[1])
            } else {
                comp.scheme = "wss"
            }
        }
        if comp.path.isEmpty { comp.path = "/ws" }
        let tk = token.isEmpty ? (urlToken ?? "") : token
        var items: [URLQueryItem] = []
        if !tk.isEmpty { items.append(URLQueryItem(name: "token", value: tk)) }
        if !sid.isEmpty { items.append(URLQueryItem(name: "sid", value: sid)) }  // 有旧会话就回挂
        if !items.isEmpty { comp.queryItems = items }
        guard let url = comp.url else {
            onClose(nil)
            return
        }
        NSLog("HERMESWS addr=%@ url=%@", addr, url.absoluteString)
        let s = URLSession(configuration: .default, delegate: self, delegateQueue: nil)
        session = s
        let t = s.webSocketTask(with: url)
        t.maximumMessageSize = 4 * 1024 * 1024   // 对齐服务端 max_msg_size
        task = t
        t.resume()
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

    private func fail(_ failedTask: URLSessionWebSocketTask) {
        guard task === failedTask, isOpen || task != nil else { return }
        isOpen = false
        task = nil
        session?.invalidateAndCancel()
        session = nil
        onClose(nil)
    }

    private func receive() {
        let receivingTask = task
        receivingTask?.receive { [weak self] result in
            guard let self, let receivingTask, self.task === receivingTask else { return }
            switch result {
            case .success(let msg):
                switch msg {
                case .string(let s): self.onText(s)
                case .data(let d): self.onBinary(d)
                @unknown default: break
                }
                self.receive()
            case .failure:
                self.fail(receivingTask)
            }
        }
    }
}

extension HermesWSClient: URLSessionWebSocketDelegate {
    // 握手完成才算连通：resume 后立刻 sendPing 在 iOS 16 上会报
    // "Socket is not connected" 把刚建好的连接判死，导致连上即断的死循环。
    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didOpenWithProtocol protocol: String?) {
        guard webSocketTask === task else { return }
        NSLog("HERMESWS open")
        isOpen = true
        onOpen()
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode,
                    reason: Data?) {
        guard webSocketTask === task else { return }
        NSLog("HERMESWS close code=%d", closeCode.rawValue)
        let wasOpen = isOpen
        isOpen = false
        task = nil
        if wasOpen {
            onClose(closeCode == .invalid ? nil : closeCode.rawValue)
        }
    }
}
