// 对话界面：按 web/index.html 逐块对齐（顶栏两行 / 状态球 / 字幕流 / 输入栏）。
// 配色与尺寸取自网页版 CSS：--bg #0d1117、--panel #161b22、--line #21262d、
// --fg #e6edf3、--dim #8b949e、--user #1f6feb、--err #f85149；
// 状态球 110pt 径向渐变，可听=蓝、思考/播报=紫红。

import SwiftUI

private enum Theme {
    static let bg = Color(hex: 0x0D1117)
    static let panel = Color(hex: 0x161B22)
    static let line = Color(hex: 0x21262D)
    static let fg = Color(hex: 0xE6EDF3)
    static let dim = Color(hex: 0x8B949E)
    static let user = Color(hex: 0x1F6FEB)
    static let err = Color(hex: 0xF85149)
    static let blue1 = Color(hex: 0x58A6FF)
    static let blue2 = Color(hex: 0x1F6FEB)
    static let mag1 = Color(hex: 0xD977FF)
    static let mag2 = Color(hex: 0xBC5CFF)
}

private extension Color {
    init(hex: UInt32) {
        self.init(.sRGB,
                  red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255,
                  opacity: 1)
    }
}

struct ChatView: View {
    @ObservedObject var vm: ChatViewModel
    var onSettings: () -> Void
    @State private var textIn = ""
    @State private var showRestartConfirm = false
    @State private var breath = false
    @Environment(\.scenePhase) private var scenePhase

    private static let stateLabel: [String: String] = [
        "idle": "已暂停",
        "listening": "在听，你说",
        "thinking": "Hermes 思考中…",
        "speaking": "播放回答（开口可打断）",
    ]

    private static let voices: [(String, String)] = [
        ("zh-CN-XiaoxiaoNeural", "晓晓（女）"),
        ("zh-CN-YunxiNeural", "云希（男）"),
        ("zh-CN-XiaoyiNeural", "晓伊（女）"),
        ("zh-CN-YunjianNeural", "云健（男）"),
    ]
    private static let rates: [(String, String)] = [
        ("+0%", "语速 1.0x"), ("+15%", "语速 1.15x"), ("+30%", "语速 1.3x"), ("-15%", "语速 0.85x"),
    ]

    private var isWorking: Bool { vm.state == "thinking" || vm.state == "speaking" }

    var body: some View {
        VStack(spacing: 0) {
            header
            stage
            transcriptView
            footer
        }
        .background(Theme.bg.ignoresSafeArea())
        .onAppear {
            if !vm.isLive { vm.toggle() }   // 打开即聊（对齐网页版 autostart）
        }
        .onChange(of: scenePhase) { phase in
            // 一直录音，不放后台：切出即停，回来自动续上。
            switch phase {
            case .background: vm.pauseForBackground()
            case .active: vm.resumeAfterForeground()
            default: break
            }
        }
        .onChange(of: vm.state) { s in
            breath = false
            guard s == "thinking" || s == "speaking" else { return }
            withAnimation(.easeInOut(duration: s == "speaking" ? 0.9 : 1.4)
                .repeatForever(autoreverses: true)) { breath = true }
        }
    }

    // MARK: - 顶栏（两行，同网页 header）

    private var header: some View {
        VStack(spacing: 8) {
            HStack(spacing: 8) {
                Text("Hermes-live")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundColor(Theme.fg)
                Text(vm.statusText)
                    .font(.system(size: 13))
                    .foregroundColor(Theme.dim)
                    .lineLimit(1)
                Spacer(minLength: 8)
                Button(action: onSettings) {
                    Image(systemName: "gearshape.fill")
                        .font(.system(size: 13))
                        .foregroundColor(Theme.dim)
                }
                Button(vm.isLive ? "停止" : "开始对话") { vm.toggle() }
                    .buttonStyle(ChipButtonStyle(primary: true))
            }
            HStack(spacing: 8) {
                Menu {
                    ForEach(Self.voices, id: \.0) { v in
                        Button(v.1) {
                            vm.voice = v.0
                            vm.pushConfig()
                        }
                    }
                } label: {
                    MenuChip(text: Self.voices.first { $0.0 == vm.voice }?.1 ?? "音色")
                }
                Menu {
                    ForEach(Self.rates, id: \.0) { r in
                        Button(r.1) {
                            vm.rate = r.0
                            vm.pushConfig()
                        }
                    }
                } label: {
                    MenuChip(text: Self.rates.first { $0.0 == vm.rate }?.1 ?? "语速")
                }
                Spacer(minLength: 4)
                Button("打断") { vm.interrupt() }
                    .buttonStyle(ChipButtonStyle())
                    .disabled(!isWorking)
                Button("新会话") { vm.newSession() }
                    .buttonStyle(ChipButtonStyle())
                Button("重启服务") { showRestartConfirm = true }
                    .buttonStyle(ChipButtonStyle())
                    .confirmationDialog("确定要重启服务吗？重启期间会短暂断开连接。",
                                        isPresented: $showRestartConfirm, titleVisibility: .visible) {
                        Button("重启", role: .destructive) { vm.restartServer() }
                        Button("取消", role: .cancel) {}
                    }
            }
        }
        .padding(.horizontal, 12)
        .padding(.top, 8)
        .padding(.bottom, 10)
        .background(Theme.bg)
        .overlay(alignment: .bottom) {
            Rectangle().fill(Theme.line).frame(height: 1)
        }
    }

    // MARK: - 状态球 + 状态文案 + 半句识别

    private var orbGradient: RadialGradient {
        let colors: [Color]
        switch vm.state {
        case "listening": colors = [Theme.blue1, Theme.blue2]
        case "thinking", "speaking": colors = [Theme.mag1, Theme.mag2]
        default: colors = [Theme.mag1.opacity(0.45), Theme.mag2.opacity(0.45)]
        }
        return RadialGradient(colors: colors, center: UnitPoint(x: 0.32, y: 0.30),
                              startRadius: 4, endRadius: 90)
    }

    private var orbGlow: Color {
        switch vm.state {
        case "listening": return Theme.blue1.opacity(0.33)
        case "speaking": return Theme.mag2.opacity(0.40)
        case "thinking": return Theme.mag2.opacity(0.22)
        default: return .clear
        }
    }

    private var stage: some View {
        VStack(spacing: 12) {
            Button { vm.toggle() } label: {
                Circle()
                    .fill(orbGradient)
                    .frame(width: 110, height: 110)
                    .scaleEffect(vm.state == "listening"
                                 ? 1 + min(0.10, CGFloat(vm.orbLevel) * 1.2)
                                 : (breath ? 1.08 : 1.0))
                    .shadow(color: orbGlow, radius: 24)
                    .animation(.easeOut(duration: 0.12), value: vm.orbLevel)
            }
            .buttonStyle(.plain)

            Text(Self.stateLabel[vm.state] ?? vm.state)
                .font(.system(size: 13))
                .foregroundColor(Theme.dim)
                .frame(minHeight: 20)

            Text(vm.partialText)
                .font(.system(size: 14))
                .italic()
                .foregroundColor(Theme.fg)
                .opacity(vm.partialText.isEmpty ? 0 : 0.7)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 340, minHeight: 20)
        }
        .padding(.top, 20)
        .padding(.bottom, 8)
    }

    // MARK: - 字幕流

    private var transcriptView: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    ForEach(vm.transcript) { msg in
                        bubble(msg).id(msg.id)
                    }
                }
                .padding(.horizontal, 18)
                .padding(.top, 10)
                .padding(.bottom, 18)
                .frame(maxWidth: 780, alignment: .leading)
                .frame(maxWidth: .infinity)
            }
            .frame(maxHeight: .infinity)
            .onChange(of: vm.transcript.count) { _ in
                if let last = vm.transcript.last { proxy.scrollTo(last.id, anchor: .bottom) }
            }
        }
    }

    @ViewBuilder
    private func bubble(_ msg: ChatMsg) -> some View {
        switch msg.kind {
        case .user:
            HStack {
                Spacer(minLength: 40)
                Text(msg.text)
                    .font(.system(size: 15))
                    .foregroundColor(Theme.fg)
                    .padding(.horizontal, 13).padding(.vertical, 9)
                    .background(Theme.user)
                    .clipShape(RoundedRectangle(cornerRadius: 12))
            }
        case .agent:
            HStack {
                Text(msg.text)
                    .font(.system(size: 15))
                    .foregroundColor(Theme.fg)
                    .padding(.horizontal, 13).padding(.vertical, 9)
                    .background(Theme.panel)
                    .overlay(RoundedRectangle(cornerRadius: 12).stroke(Theme.line, lineWidth: 1))
                    .clipShape(RoundedRectangle(cornerRadius: 12))
                Spacer(minLength: 40)
            }
        case .tool:
            Text(msg.text)
                .font(.system(size: 12.5))
                .foregroundColor(Theme.dim)
                .padding(.horizontal, 4)
        case .error:
            HStack {
                Spacer()
                Text(msg.text)
                    .font(.system(size: 13))
                    .foregroundColor(Theme.err)
                Spacer()
            }
        }
    }

    // MARK: - 输入栏

    private var footer: some View {
        HStack(spacing: 8) {
            TextField("也可以打字（回车发送）…", text: $textIn)
                .font(.system(size: 14))
                .foregroundColor(Theme.fg)
                .textFieldStyle(.plain)
                .padding(.horizontal, 12).padding(.vertical, 8)
                .background(Theme.panel)
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(Theme.line, lineWidth: 1))
                .clipShape(RoundedRectangle(cornerRadius: 8))
                .submitLabel(.send)
                .onSubmit(send)
            Button("发送", action: send)
                .buttonStyle(ChipButtonStyle())
                .disabled(textIn.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .frame(maxWidth: 780)
        .frame(maxWidth: .infinity)
        .overlay(alignment: .top) {
            Rectangle().fill(Theme.line).frame(height: 1)
        }
    }

    private func send() {
        let t = textIn.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        vm.sendText(t)
        textIn = ""
    }
}

// MARK: - 控件样式（对齐网页 button / select）

private struct ChipButtonStyle: ButtonStyle {
    var primary = false
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12.5))
            .lineLimit(1)
            .fixedSize(horizontal: true, vertical: false)
            .foregroundColor(primary ? .white : Theme.fg)
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(primary ? Theme.user : Theme.panel)
            .overlay(RoundedRectangle(cornerRadius: 8)
                .stroke(primary ? Theme.user : Theme.line, lineWidth: 1))
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .opacity(isEnabled ? (configuration.isPressed ? 0.7 : 1) : 0.4)
    }
}

private struct MenuChip: View {
    let text: String

    var body: some View {
        HStack(spacing: 4) {
            Text(text).font(.system(size: 12.5)).foregroundColor(Theme.fg).lineLimit(1)
            Image(systemName: "chevron.down")
                .font(.system(size: 9)).foregroundColor(Theme.dim)
        }
        .padding(.horizontal, 8).padding(.vertical, 6)
        .background(Theme.panel)
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Theme.line, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: 8))
    }
}
