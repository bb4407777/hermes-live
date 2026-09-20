// Hermes-Live iOS 原生端：AVAudioEngine 录音/播放，WebSocket 直连 Mac 的 8698。
// 后台运行靠 UIBackgroundModes=audio（project.yml 已声明），对话中锁屏/切后台不断线。
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
    @State private var showSettings = false

    var body: some View {
        ZStack {
            Color(red: 0.05, green: 0.07, blue: 0.09).ignoresSafeArea()
            if serverAddr.isEmpty || showSettings {
                SettingsView(serverAddr: $serverAddr, serverToken: $serverToken) {
                    showSettings = false
                }
            } else {
                // 地址/Token 变化 → 换 id 重建 ViewModel（新连接新会话）
                ChatView(
                    vm: ChatViewModel(serverAddr: serverAddr, serverToken: serverToken),
                    onSettings: { showSettings = true }
                )
                .id(serverAddr + "|" + serverToken)
            }
        }
        .preferredColorScheme(.dark)
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
