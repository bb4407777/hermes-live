"""变异测试：把三个音频客户端逐条改坏，确认 tests/worklet_sim.mjs 真的会红。

仿真是这轮唯一能覆盖 AudioWorklet 的手段，但"测试全绿"本身不说明它有用——
所以这里反向验证：每一种已知的坏改法（去掉水位、去掉 end 收口、去掉 turn 取模、
去掉越界夹取……）都必须被至少一条断言抓到。哪天有人把断言写松了，这里先红。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SIM = ROOT / "tests" / "worklet_sim.mjs"
PLAYER = ROOT / "web" / "worklets" / "player-processor.js"
MIC = ROOT / "web" / "worklets" / "mic-processor.js"
MP = ROOT / "miniprogram" / "utils" / "audio.js"
# 每个目标文件对应仿真脚本里的一个环境变量覆盖位（空值 = 用仓库里的原件）
ENVVAR = {PLAYER: "HL_PLAYER_WORKLET", MIC: "HL_MIC_WORKLET", MP: "HL_MP_AUDIO"}

# (名字, 目标文件, 原文, 改成) —— 原文必须命中，否则测试直接失败（防止改动后悄悄失配）
MUTANTS = [
    ("预灌水位形同虚设", PLAYER, "const PREFILL_MS = 120;", "const PREFILL_MS = 0;"),
    ("预灌门不再累计欠载（结尾不上报播完）", PLAYER,
     "        this._idle();\n        return true;", "        return true;"),
    ("_next 不规范化读位置（跨块 NaN）", PLAYER,
     "while (this.chunks.length && this.pos >= this.chunks[0].length)",
     "while (false && this.pos >= 0)"),
    ("音频不按 turn 过滤（打断后旧音频照播）", PLAYER,
     "        if (d.turn !== this.turn) return;", ""),
    ("done 不再要求 tts_end（句间空隙误判播完）", PLAYER,
     "if (this.ended && this.playedAny && !this.notifiedDone",
     "if (this.playedAny && !this.notifiedDone"),
    ("帧头 turn 不取模（第 256 轮起静音）", PLAYER,
     "const t = d.turn & 0xFF;", "const t = d.turn;"),
    ("同一 turn 重复 setTurn 仍清缓冲（整轮被啃成碎片）", PLAYER,
     "if (t !== this.turn) {\n          this.turn = t;\n          this._wipe();",
     "if (true) {\n          this.turn = t;\n          this._wipe();"),
    ("录音残留 keep 不夹取（首个量子就 RangeError，麦克风全哑）", MIC,
     "const keep = Math.min(pos | 0, n - 1);", "const keep = pos | 0;"),
    ("上行重采样倍率写错", MIC,
     "this.ratio = sampleRate / 16000;", "this.ratio = sampleRate / 8000;"),
    ("上行不做削波（溢出回绕成噪音）", MIC,
     "s < -1 ? -32768 : (s > 0.99997 ? 32767 : (s * 32768) | 0)", "(s * 32768) | 0"),
    ("上行帧长写错", MIC, "const OUT_SAMPLES = 512;", "const OUT_SAMPLES = 256;"),
    # 小程序端 Player 与 web worklet 是两套实现，同一类坑要各证一次（仿真里也各有一段）
    ("小程序 setTurn 无条件清缓冲（同 turn 的 listening 掐掉尾音）", MP,
     "if (turn === this.turn) return;", ""),
]

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")


@pytest.mark.parametrize("name,target,bad,good", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_mutant_is_caught(name, target, bad, good, tmp_path):
    src = target.read_text(encoding="utf-8")
    assert bad in src, f"{name}：变异锚点已在 {target.name} 里找不到，仿真断言可能也过期了"
    mutant = tmp_path / target.name
    mutant.write_text(src.replace(bad, good), encoding="utf-8")
    assert mutant.read_text(encoding="utf-8") != src, f"「{name}」的替换没改动任何字节，是个空变异"
    env = dict(PATH="/usr/bin:/bin:/opt/homebrew/bin",
               **{var: str(mutant) if target is tgt else "" for tgt, var in ENVVAR.items()})
    r = subprocess.run(["node", str(SIM)], capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode != 0, f"改坏「{name}」后仿真仍然全绿 —— 断言不够严"
