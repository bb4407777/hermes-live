// Hermes-Live 菜单栏壳：NSStatusItem + NSPopover 内嵌 WKWebView 指向本地 8698。
// 壳只做三件事：拉起/守护 python 服务、承载网页、放行麦克风。UI 全在 web/ 里，零重写。
// 构建：macos/build-app.sh（免费 Xcode CLT + ad-hoc 签名，无需付费开发者账号）。

import AppKit
import WebKit

let SERVER_URL = URL(string: "http://127.0.0.1:8698")!
let PROJECT_DIR = ProcessInfo.processInfo.environment["HERMES_LIVE_DIR"]
    ?? "\(NSHomeDirectory())/Code/hermes-live"

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate {
    var statusItem: NSStatusItem!
    var popover = NSPopover()
    var webView: WKWebView!
    var serverProcess: Process?      // 仅当由壳拉起时非 nil；退出时只杀自己拉的
    var healthTimer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        setupStatusItem()
        setupWebView()
        ensureServer()
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let p = serverProcess, p.isRunning { p.terminate() }
    }

    // MARK: 菜单栏

    private func setupStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "waveform.circle",
                                   accessibilityDescription: "Hermes-Live")
            button.action = #selector(statusItemClicked)
            button.target = self
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
    }

    @objc private func statusItemClicked() {
        if NSApp.currentEvent?.type == .rightMouseUp {
            showMenu()
            return
        }
        if popover.isShown {
            popover.performClose(nil)
        } else if let button = statusItem.button {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            popover.contentViewController?.view.window?.makeKey()
        }
    }

    private func showMenu() {
        let menu = NSMenu()
        menu.addItem(withTitle: "在浏览器打开", action: #selector(openInBrowser), keyEquivalent: "")
        menu.addItem(withTitle: "重新加载页面", action: #selector(reloadPage), keyEquivalent: "r")
        menu.addItem(.separator())
        menu.addItem(withTitle: "退出 Hermes-Live", action: #selector(quit), keyEquivalent: "q")
        menu.items.forEach { $0.target = self }
        statusItem.menu = menu
        statusItem.button?.performClick(nil)
        statusItem.menu = nil   // 用完摘掉，恢复左键 action
    }

    @objc private func openInBrowser() { NSWorkspace.shared.open(SERVER_URL) }
    @objc private func reloadPage() { webView.reload() }
    @objc private func quit() { NSApp.terminate(nil) }

    // MARK: WebView（弹窗关闭时 view 仍存活，对话/音频不断）

    private func setupWebView() {
        let config = WKWebViewConfiguration()
        config.mediaTypesRequiringUserActionForPlayback = []   // 允许 AudioContext 自动出声
        webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 420, height: 680),
                            configuration: config)
        webView.uiDelegate = self

        let vc = NSViewController()
        vc.view = webView
        popover.contentViewController = vc
        popover.contentSize = NSSize(width: 420, height: 680)
        popover.behavior = .transient
        webView.loadHTMLString(placeholderHTML("正在启动语音服务…（首次约 20 秒）"), baseURL: nil)
    }

    // 网页 getUserMedia → 放行（TCC 系统弹窗仍由 macOS 把关，Info.plist 已带用途声明）
    func webView(_ webView: WKWebView,
                 requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo,
                 type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(.grant)
    }

    // MARK: 服务守护

    private func ensureServer() {
        healthCheck { [weak self] ok in
            guard let self else { return }
            if ok {
                self.loadApp()
            } else {
                self.spawnServer()
                self.waitForServer(deadline: Date().addingTimeInterval(60))
            }
        }
    }

    private func spawnServer() {
        let python = "\(PROJECT_DIR)/.venv/bin/python"
        guard FileManager.default.isExecutableFile(atPath: python) else {
            webView.loadHTMLString(placeholderHTML("找不到 \(python)<br>请先按 README 建好 venv"), baseURL: nil)
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = ["-m", "server.main"]
        p.currentDirectoryURL = URL(fileURLWithPath: PROJECT_DIR)
        p.standardOutput = FileHandle.nullDevice
        p.standardError = try? FileHandle(forWritingTo:
            URL(fileURLWithPath: "\(PROJECT_DIR)/tmp/server-shell.log"))
            ?? FileHandle.nullDevice
        do {
            try p.run()
            serverProcess = p
        } catch {
            webView.loadHTMLString(placeholderHTML("语音服务启动失败：\(error.localizedDescription)"), baseURL: nil)
        }
    }

    private func waitForServer(deadline: Date) {
        healthTimer?.invalidate()
        healthTimer = Timer.scheduledTimer(withTimeInterval: 1.5, repeats: true) { [weak self] timer in
            guard let self else { timer.invalidate(); return }
            self.healthCheck { ok in
                if ok {
                    timer.invalidate()
                    self.loadApp()
                } else if Date() > deadline {
                    timer.invalidate()
                    self.webView.loadHTMLString(
                        self.placeholderHTML("语音服务 60 秒内未就绪，看 tmp/server-shell.log"), baseURL: nil)
                }
            }
        }
    }

    private func healthCheck(_ done: @escaping (Bool) -> Void) {
        var req = URLRequest(url: SERVER_URL.appendingPathComponent("api/health"))
        req.timeoutInterval = 2
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            let ok = (resp as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async { done(ok) }
        }.resume()
    }

    private func loadApp() {
        webView.load(URLRequest(url: SERVER_URL))
    }

    private func placeholderHTML(_ text: String) -> String {
        """
        <html><body style="background:#0d1117;color:#8b949e;font:15px -apple-system;
        display:flex;align-items:center;justify-content:center;height:95vh;text-align:center">
        <div>\(text)</div></body></html>
        """
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
