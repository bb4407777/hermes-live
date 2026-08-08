// Hermes-Live iOS 壳：回环服务器托管仓库 web/ 页面，WebSocket 连回 Mac 的 8698。
// 免费个人签自用（签名 7 天有效，过期连 Xcode 重装一次）。步骤见 ios/README-ios.md。

import SwiftUI

@main
struct HermesLiveApp: App {
    var body: some Scene {
        WindowGroup { RootView() }
    }
}

struct RootView: View {
    @AppStorage("serverAddr") private var serverAddr = ""
    @AppStorage("serverToken") private var serverToken = ""
    @State private var loopbackPort: UInt16 = 0
    @State private var showSettings = false
    @State private var reloadKey = 0
    private let server = LoopbackServer()

    var body: some View {
        ZStack(alignment: .topTrailing) {
            Color(red: 0.05, green: 0.07, blue: 0.09).ignoresSafeArea()
            if serverAddr.isEmpty || showSettings {
                SettingsView(serverAddr: $serverAddr, serverToken: $serverToken) {
                    showSettings = false
                    reloadKey += 1
                }
            } else if loopbackPort > 0, let url = pageURL() {
                WebView(url: url)
                    .id(reloadKey)          // 改设置后整个重载
                    .ignoresSafeArea(edges: .bottom)
                Button { showSettings = true } label: {
                    Image(systemName: "gearshape.fill")
                        .foregroundColor(.gray).padding(10)
                }
            } else {
                ProgressView().tint(.gray)
            }
        }
        .onAppear {
            AudioSessionHelper.activate()
            UIApplication.shared.isIdleTimerDisabled = true   // 对话中不锁屏
            if loopbackPort == 0 {
                server.start { port in loopbackPort = port }
            }
        }
    }

    private func pageURL() -> URL? {
        var comp = URLComponents()
        comp.scheme = "http"
        comp.host = "127.0.0.1"
        comp.port = Int(loopbackPort)
        comp.path = "/"
        var items = [URLQueryItem(name: "server", value: serverAddr)]
        if !serverToken.isEmpty {
            items.append(URLQueryItem(name: "token", value: serverToken))
        }
        comp.queryItems = items
        return comp.url
    }
}

struct SettingsView: View {
    @Binding var serverAddr: String
    @Binding var serverToken: String
    var onDone: () -> Void

    var body: some View {
        VStack(spacing: 18) {
            Text("Hermes-Live").font(.title2).bold().foregroundColor(.white)
            Text("填 Mac 的地址。家里 Wi-Fi 用局域网 IP；\n配了 Tailscale 用 100.x 地址，外面也能连。")
                .font(.footnote).foregroundColor(.gray).multilineTextAlignment(.center)
            TextField("如 192.168.1.5:8698", text: $serverAddr)
                .textFieldStyle(.roundedBorder)
                .keyboardType(.URL)
                .autocapitalization(.none)
                .disableAutocorrection(true)
            SecureField("Token（Mac 配置了 auth_token 才填）", text: $serverToken)
                .textFieldStyle(.roundedBorder)
            Button("连接") { if !serverAddr.isEmpty { onDone() } }
                .buttonStyle(.borderedProminent)
                .disabled(serverAddr.isEmpty)
            Spacer()
        }
        .padding(24)
        .padding(.top, 40)
    }
}
