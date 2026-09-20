// 对话界面：对齐 web/index.html 的元素（状态球/字幕流/控制按钮/文字输入/音色语速）。

import SwiftUI

struct ChatView: View {
    @ObservedObject var vm: ChatViewModel
    var onSettings: () -> Void
    @State private var textIn = ""
    @State private var showRestartConfirm = false

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
        ("+0%", "1.0x"), ("+15%", "1.15x"), ("+30%", "1.3x"), ("-15%", "0.85x"),
    ]

    var body: some View {
        VStack(spacing: 0) {
            header
            orb
            transcriptView
            controls
            inputBar
        }
        .background(Color(red: 0.05, green: 0.07, blue: 0.09).ignoresSafeArea())
        .onAppear {
            if !vm.isLive { vm.toggle() }   // 打开即聊（对齐网页版 autostart）
        }
    }

    // MARK: - 顶栏：状态 + 音色/语速 + 设置

    private var header: some View {
        HStack(spacing: 10) {
            Text(vm.statusText)
                .font(.footnote).foregroundColor(.gray)
                .lineLimit(1)
            Spacer()
            Picker("音色", selection: $vm.voice) {
                ForEach(Self.voices, id: \.0) { Text($0.1).tag($0.0) }
            }
            .pickerStyle(.menu).labelsHidden().tint(.gray)
            Picker("语速", selection: $vm.rate) {
                ForEach(Self.rates, id: \.0) { Text($0.1).tag($0.0) }
            }
            .pickerStyle(.menu).labelsHidden().tint(.gray)
            Button(action: onSettings) {
                Image(systemName: "gearshape.fill").foregroundColor(.gray)
            }
        }
        .padding(.horizontal).padding(.vertical, 8)
        .onChange(of: vm.voice) { _ in pushConfig() }
        .onChange(of: vm.rate) { _ in pushConfig() }
    }

    // MARK: - 状态球（对齐网页版：可听语音=蓝、工作中=紫红）

    private var orbColor: Color {
        switch vm.state {
        case "listening": return .blue
        case "thinking", "speaking": return Color(red: 0.8, green: 0.2, blue: 0.5)
        default: return .gray.opacity(0.5)
        }
    }

    private var orb: some View {
        Button { vm.toggle() } label: {
            ZStack {
                Circle()
                    .fill(orbColor)
                    .frame(width: 96, height: 96)
                    .scaleEffect(vm.state == "listening"
                                 ? 1 + min(0.25, CGFloat(vm.orbLevel) * 2.2) : 1)
                    .animation(.easeOut(duration: 0.12), value: vm.orbLevel)
                Image(systemName: vm.isLive ? "stop.fill" : "mic.fill")
                    .font(.title).foregroundColor(.white)
            }
        }
        .padding(.vertical, 12)

        .overlay(alignment: .bottom) {
            Text(Self.stateLabel[vm.state] ?? vm.state)
                .font(.caption).foregroundColor(.gray)
                .offset(y: 18)
        }
        .padding(.bottom, 14)
    }

    // MARK: - 字幕流

    private var transcriptView: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(vm.transcript) { msg in
                        bubble(msg).id(msg.id)
                    }
                    if !vm.partialText.isEmpty {
                        Text(vm.partialText)
                            .font(.callout).foregroundColor(.gray)
                            .id("partial")
                    }
                }
                .padding(.horizontal)
            }
            .frame(maxHeight: .infinity)
            .onChange(of: vm.transcript.count) { _ in
                if let last = vm.transcript.last { proxy.scrollTo(last.id) }
            }
            .onChange(of: vm.partialText) { _ in
                proxy.scrollTo("partial")
            }
        }
    }

    @ViewBuilder
    private func bubble(_ msg: ChatMsg) -> some View {
        switch msg.kind {
        case .user:
            HStack { Spacer()
                Text(msg.text).padding(10)
                    .background(Color.blue.opacity(0.35)).cornerRadius(12)
            }
        case .agent:
            HStack { Text(msg.text).padding(10)
                    .background(Color.white.opacity(0.08)).cornerRadius(12)
                Spacer() }
        case .tool:
            Text(msg.text).font(.caption).foregroundColor(.teal)
        case .error:
            Text(msg.text).font(.caption).foregroundColor(.red)
        }
    }

    // MARK: - 控制按钮

    private var controls: some View {
        HStack(spacing: 16) {
            Button("打断") { vm.interrupt() }
                .disabled(!(vm.state == "thinking" || vm.state == "speaking"))
            Button("新会话") { vm.newSession() }
            Button("重启服务") { showRestartConfirm = true }
                .confirmationDialog("确定要重启服务吗？重启期间会短暂断开连接。",
                                    isPresented: $showRestartConfirm, titleVisibility: .visible) {
                    Button("重启", role: .destructive) { vm.restartServer() }
                    Button("取消", role: .cancel) {}
                }
        }
        .font(.callout)
        .padding(.vertical, 8)
    }

    // MARK: - 文字输入

    private var inputBar: some View {
        HStack(spacing: 8) {
            TextField("也可以打字…", text: $textIn)
                .textFieldStyle(.roundedBorder)
                .onSubmit {
                    vm.sendText(textIn)
                    textIn = ""
                }
            Button {
                vm.sendText(textIn)
                textIn = ""
            } label: {
                Image(systemName: "paperplane.fill")
            }
            .disabled(textIn.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
        }
        .padding(.horizontal).padding(.vertical, 8)
    }

    private func pushConfig() {
        // 连接后改了音色/语速即时同步（对齐 web pushConfig）
        vm.pushConfig()
    }
}
