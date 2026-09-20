// 原生音频：AVAudioEngine 采集 16k PCM16 上行 + AVAudioPlayerNode 播 24k PCM16 下行。
// 替代 web/audio.js + 两个 worklet：
//   采集 = mic-processor.js（重采样 16k、512 样本/帧、电平节流 ~8Hz）
//   播放 = player-processor.js（turn 过滤打断竞态、tts_end+排空后报 playback_done）
// 半双工：speaking/thinking 态 micPaused=true 停发帧（与网页/小程序行为对齐）。

import AVFoundation

final class AudioController {
    var onFrame: (Data) -> Void = { _ in }          // 512 样本 Int16LE（1024 字节/帧）
    var onLevel: (Float) -> Void = { _ in }         // RMS 电平，驱动状态球动画
    var onPlaybackDone: (Int) -> Void = { _ in }    // turn 播完 → 发 playback_done

    var micPaused = false

    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private var converter: AVAudioConverter?
    private var inputSampleRate: Double = 48000

    private let captureFormat = AVAudioFormat(
        commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: false)!
    private let playbackFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16, sampleRate: 24000, channels: 1, interleaved: false)!

    // 采集侧
    private var carry: [Float] = []
    private var levelTick = 0

    // 播放侧：全部走 playQueue 串行，stop() 未播 buffer 的 completion 不回调，
    // 用 gen 代数区分新旧 turn，pending 计数在 setTurn 时随 gen 清零。
    private let playQueue = DispatchQueue(label: "hermes-live.playback")
    private var currentTurn = -1
    private var gen: Int = 0
    private var pending = 0
    private var ttsEndReceived = false

    private(set) var running = false

    init() {
        super.init()
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: playbackFormat)
        NotificationCenter.default.addObserver(
            self, selector: #selector(onInterruption(_:)),
            name: AVAudioSession.interruptionNotification, object: nil)
    }

    // MARK: - 生命周期

    func start() {
        guard !running else { return }
        let session = AVAudioSession.sharedInstance()
        // voiceChat 模式自带回声消除/降噪（对齐 web 的 echoCancellation 等约束）
        try? session.setCategory(.playAndRecord, mode: .voiceChat,
                                 options: [.defaultToSpeaker, .allowBluetooth])
        try? session.setActive(true)

        let input = engine.inputNode
        let hwFormat = input.outputFormat(forBus: 0)
        inputSampleRate = hwFormat.sampleRate
        converter = AVAudioConverter(from: hwFormat, to: captureFormat)
        carry.removeAll()
        input.installTap(onBus: 0, bufferSize: 1024, format: hwFormat) { [weak self] buf, _ in
            self?.processMic(buf)
        }
        do {
            try engine.start()
            player.play()
            running = true
        } catch {
            try? session.setActive(false)
        }
    }

    func stop() {
        guard running else { return }
        running = false
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        playQueue.sync {
            gen &+= 1
            pending = 0
            ttsEndReceived = false
        }
        player.stop()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    // MARK: - 采集：硬件格式 → 16k Float32 → 512 样本 Int16 帧

    private func processMic(_ buf: AVAudioPCMBuffer) {
        guard running, let converter else { return }
        let capacity = AVAudioFrameCount(
            Double(buf.frameLength) * 16000.0 / inputSampleRate) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: captureFormat, frameCapacity: capacity)
        else { return }
        var consumed = false
        var error: NSError?
        converter.convert(to: out, error: &error) { _, status in
            if consumed {
                status.pointee = .noDataNow
                return nil
            }
            consumed = true
            status.pointee = .haveData
            return buf
        }
        let n = Int(out.frameLength)
        guard error == nil, n > 0, let ch = out.floatChannelData?[0] else { return }

        // 电平节流 ~8Hz（48k/1024 ≈ 47 次/秒，每 6 次报一次）
        levelTick += 1
        if levelTick >= 6 {
            levelTick = 0
            var sum: Float = 0
            for i in 0..<n { sum += ch[i] * ch[i] }
            let level = sqrtf(sum / Float(n))
            DispatchQueue.main.async { [weak self] in self?.onLevel(level) }
        }

        carry.append(contentsOf: UnsafeBufferPointer(start: ch, count: n))
        while carry.count >= 512 {
            if !micPaused {
                var frame = Data(count: 1024)
                frame.withUnsafeMutableBytes { ptr in
                    let dst = ptr.bindMemory(to: Int16.self)
                    for i in 0..<512 {
                        let s = max(-1.0, min(1.0, carry[i]))
                        dst[i] = Int16(s * 32767)   // arm64 小端 = PCM16LE
                    }
                }
                onFrame(frame)
            }
            carry.removeFirst(512)
        }
    }

    // MARK: - 播放：turn 过滤 + tts_end 且排空后报 done

    /// 状态切换带新 turn：清播放缓冲，旧 turn 在途帧自然作废（对齐 web setTurn）
    func setTurn(_ turn: Int) {
        playQueue.async {
            guard turn != self.currentTurn else { return }
            self.currentTurn = turn
            self.gen &+= 1
            self.pending = 0
            self.ttsEndReceived = false
            self.player.stop()
            if self.running { self.player.play() }
        }
    }

    func enqueue(turn: Int, pcm: Data) {
        playQueue.async {
            guard self.running, turn == self.currentTurn else { return }
            let frames = pcm.count / 2
            guard frames > 0,
                  let buf = AVAudioPCMBuffer(pcmFormat: self.playbackFormat,
                                             frameCapacity: AVAudioFrameCount(frames))
            else { return }
            buf.frameLength = AVAudioFrameCount(frames)
            if let dst = buf.int16ChannelData?[0] {
                pcm.copyBytes(to: UnsafeMutableBufferPointer(start: dst, count: frames))
            }
            self.pending += 1
            let gen = self.gen
            self.player.scheduleBuffer(buf, completionCallbackType: .dataPlayedBack) { [weak self] _ in
                self?.playQueue.async {
                    guard let self, gen == self.gen else { return }
                    self.pending -= 1
                    self.checkDone()
                }
            }
        }
    }

    /// 服务端 tts_end：本 turn 音频发完。等已排队的 buffer 播完再报 playback_done。
    func markTtsEnd(turn: Int) {
        playQueue.async {
            guard turn == self.currentTurn else { return }
            self.ttsEndReceived = true
            self.checkDone()
        }
    }

    private func checkDone() {   // 仅在 playQueue 上调用
        guard ttsEndReceived, pending == 0 else { return }
        ttsEndReceived = false
        let turn = currentTurn
        DispatchQueue.main.async { [weak self] in self?.onPlaybackDone(turn) }
    }

    // MARK: - 电话/其他音频打断

    @objc private func onInterruption(_ note: Notification) {
        guard let info = note.userInfo,
              let raw = info[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
        guard type == .ended, running else { return }   // began：引擎已被系统挂起，无需处理
        let opts = info[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0
        guard AVAudioSession.InterruptionOptions(rawValue: opts).contains(.shouldResume)
        else { return }
        // 打断结束后引擎需重新启动（系统不会自动恢复 input tap）
        do {
            try AVAudioSession.sharedInstance().setActive(true)
            try engine.start()
            player.play()
        } catch { /* 恢复失败：下次 start/stop 重建 */ }
    }
}
