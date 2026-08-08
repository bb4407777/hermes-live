// 手机回环静态服务器：把 bundle 里的 web/ 用 http://127.0.0.1:<port> 端出来。
// 为什么需要它：iOS 的 getUserMedia 只在安全上下文可用，http 回环恰好是安全上下文，
// 而直接打开 http://<Mac IP> 不是——所以页面从本机回环加载，WebSocket 再连回 Mac。

import Foundation
import Network

final class LoopbackServer {
    private var listener: NWListener?
    private(set) var port: UInt16 = 0

    func start(completion: @escaping (UInt16) -> Void) {
        do {
            let params = NWParameters.tcp
            params.requiredLocalEndpoint = NWEndpoint.hostPort(host: "127.0.0.1", port: .any)
            let l = try NWListener(using: params)
            listener = l
            l.stateUpdateHandler = { [weak self] state in
                if case .ready = state, let p = l.port?.rawValue {
                    self?.port = p
                    DispatchQueue.main.async { completion(p) }
                }
            }
            l.newConnectionHandler = { [weak self] conn in self?.serve(conn) }
            l.start(queue: .global(qos: .userInitiated))
        } catch {
            NSLog("LoopbackServer start failed: \(error)")
        }
    }

    private func serve(_ conn: NWConnection) {
        conn.start(queue: .global(qos: .userInitiated))
        receiveRequest(conn, buffer: Data())
    }

    private func receiveRequest(_ conn: NWConnection, buffer: Data) {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 16384) { [weak self] data, _, done, error in
            guard let self, error == nil, let data else { conn.cancel(); return }
            var buf = buffer
            buf.append(data)
            if let headerEnd = buf.range(of: Data("\r\n\r\n".utf8)) {
                let head = String(data: buf[..<headerEnd.lowerBound], encoding: .utf8) ?? ""
                self.respond(conn, requestHead: head)
            } else if done || buf.count > 65536 {
                conn.cancel()
            } else {
                self.receiveRequest(conn, buffer: buf)
            }
        }
    }

    private func respond(_ conn: NWConnection, requestHead: String) {
        let parts = requestHead.components(separatedBy: " ")
        guard parts.count >= 2, parts[0] == "GET" else {
            send(conn, status: "405 Method Not Allowed", type: "text/plain", body: Data("nope".utf8))
            return
        }
        var path = parts[1]
        if let q = path.firstIndex(of: "?") { path = String(path[..<q]) }
        if path == "/" { path = "/index.html" }
        // 页面在 bundle 的 web/ 下；/web/xxx 与 /xxx 都映射进去，禁止路径逃逸
        var rel = path.hasPrefix("/web/") ? String(path.dropFirst(5)) : String(path.dropFirst(1))
        rel = rel.removingPercentEncoding ?? rel
        guard !rel.contains("..") else {
            send(conn, status: "403 Forbidden", type: "text/plain", body: Data("no".utf8))
            return
        }
        guard let base = Bundle.main.resourceURL?.appendingPathComponent("web"),
              let data = try? Data(contentsOf: base.appendingPathComponent(rel)) else {
            send(conn, status: "404 Not Found", type: "text/plain", body: Data("not found".utf8))
            return
        }
        send(conn, status: "200 OK", type: mime(for: rel), body: data)
    }

    private func mime(for path: String) -> String {
        if path.hasSuffix(".html") { return "text/html; charset=utf-8" }
        if path.hasSuffix(".js") { return "text/javascript; charset=utf-8" }
        if path.hasSuffix(".css") { return "text/css; charset=utf-8" }
        if path.hasSuffix(".png") { return "image/png" }
        return "application/octet-stream"
    }

    private func send(_ conn: NWConnection, status: String, type: String, body: Data) {
        var head = "HTTP/1.1 \(status)\r\n"
        head += "Content-Type: \(type)\r\n"
        head += "Content-Length: \(body.count)\r\n"
        head += "Cache-Control: no-cache\r\n"
        head += "Connection: close\r\n\r\n"
        var out = Data(head.utf8)
        out.append(body)
        conn.send(content: out, completion: .contentProcessed { _ in conn.cancel() })
    }
}
