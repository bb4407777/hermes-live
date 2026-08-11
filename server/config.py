"""集中配置：默认值 → config.yaml 覆盖 → 环境变量覆盖（HERMES_LIVE_*）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    # 服务
    host: str = "127.0.0.1"     # 手机接入时改 0.0.0.0（务必同时设 auth_token）
    port: int = 8698
    auth_token: str = ""        # 非空时 /ws 必须带 ?token=；只监听 127.0.0.1 可留空

    # Hermes gateway（只读依赖，绝不重启/修改它）
    hermes_base_url: str = "http://127.0.0.1:8647"
    hermes_api_key: str = "hermes-local-key"
    hermes_model: str = "k3"
    # 每轮叠加的临时 system 提示（网关将其层叠在核心 prompt 之上，不改 Hermes 配置）
    voice_system_prompt: str = (
        "你正在和用户进行实时语音对话。回答务必口语化、简短直接，"
        "默认三五句话说完；不用 markdown、不列清单、不贴代码。"
        "用中文回答，除非用户要求其他语言。"
    )

    # 音频
    in_rate: int = 16000          # 上行采样率（whisper/silero 原生）
    out_rate: int = 24000         # 下行采样率（edge-tts 原生）
    frame_samples: int = 512      # 512 样本 @16k = 32ms，等于 pysilero chunk 尺寸

    # VAD / 分段
    vad_threshold: float = 0.5          # listening 档语音概率阈值
    vad_start_frames: int = 6           # 连续 6 帧（192ms）判入语音段
    vad_end_silence_ms: int = 800       # 尾静音多久判段结束（原 1200；800ms 平衡中文停顿与响应速度）
    vad_min_utterance_ms: int = 300     # 短于此丢弃（咳嗽/键盘）
    vad_max_utterance_ms: int = 30000   # 强制截断
    vad_preroll_ms: int = 300           # 语音段开头回补

    # barge-in（speaking 态打断档）
    barge_threshold: float = 0.85
    barge_hold_ms: int = 320
    barge_cooldown_ms: int = 450
    barge_preroll_ms: int = 500

    # ASR
    asr_backend: str = "auto"           # auto | doubao | sherpa | whispercpp | faster
    # doubaoime 豆包逆向云端 ASR（优先级最高；需联网；依赖 doubaoime-asr 包）
    asr_doubao_credential_path: str = str(Path.home() / ".config/doubao-asr/credentials.json")
    # sherpa-onnx 流式 paraformer（优先；模型与 expression-trainer 共用，无需另行下载）
    asr_sherpa_model_dir: str = "/Users/gao/clone/expression-trainer/models/sherpa-onnx-streaming-paraformer-bilingual-zh-en"
    asr_sherpa_threads: int = 4
    asr_ggml_model: str = "models/ggml/ggml-large-v3-turbo-q5_0.bin"
    asr_threads: int = 6
    asr_model: str = "large-v3-turbo"   # faster-whisper 档位，可降 "small" 省内存
    asr_compute_type: str = "int8"
    asr_language: str = "zh"
    asr_beam_size: int = 1
    asr_initial_prompt: str = (
        "以下是律师事务所的普通话工作对话，说话人可能提及案件当事人、法院、合同条款等法律词汇，"
        "也可能谈及日常事务。"
        # 注：结尾禁放祈使句（如"请准确转写。"）——whisper 在静音/噪声段会复读提示词尾巴当转写结果
    )
    asr_no_speech_prob_max: float = 0.5  # 原 0.6，收紧后静音段更容易被过滤
    asr_avg_logprob_min: float = -1.2
    asr_download_root: str = str(PROJECT_ROOT / "models")

    # TTS
    tts_backend: str = "auto"           # auto=kokoro优先,edge-tts兜底 | kokoro=强制本地 | edge=强制云端
    tts_voice: str = "zh-CN-XiaoxiaoNeural"  # edge-tts 音色
    tts_rate: str = "+0%"              # edge-tts 语速
    kokoro_model: str = str(Path(__file__).resolve().parent.parent / "models/kokoro/kokoro-v1.1-zh.onnx")
    kokoro_voices: str = str(Path(__file__).resolve().parent.parent / "models/kokoro/voices-v1.1-zh.bin")
    kokoro_voice: str = "zf_001"       # kokoro 音色（zf_001=普通话女声）
    kokoro_speed: float = 1.0          # kokoro 语速（0.5–2.0）
    tts_lookahead: int = 1              # 预合成句数

    # 分句
    sentence_max_buffer: int = 50       # 缓冲超过此长度时逗号也可切
    sentence_first_min: int = 10        # 首句加速：≥10 字遇逗号即切

    # 面板是否显示 Hermes 的工具执行过程（如"上班打卡"式 ls——高律师 2026-08-08 定默认不看）
    show_tool_progress: bool = False

    extra: dict = field(default_factory=dict)


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg = Config()
    yaml_path = Path(path) if path else PROJECT_ROOT / "config.yaml"
    if yaml_path.exists():
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        known = {f.name for f in fields(Config)}
        for k, v in data.items():
            if k in known:
                setattr(cfg, k, v)
            else:
                cfg.extra[k] = v
    for f in fields(Config):
        env = os.environ.get(f"HERMES_LIVE_{f.name.upper()}")
        if env is not None:
            cur = getattr(cfg, f.name)
            if isinstance(cur, bool):
                setattr(cfg, f.name, env.lower() in ("1", "true", "yes"))
            elif isinstance(cur, int):
                setattr(cfg, f.name, int(env))
            elif isinstance(cur, float):
                setattr(cfg, f.name, float(env))
            elif isinstance(cur, str):
                setattr(cfg, f.name, env)
    return cfg
