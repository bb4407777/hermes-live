"""音频 worklet 的离线仿真入口：真正的断言在 tests/worklet_sim.mjs 里。

放进 pytest 是为了让 `pytest tests/ -q` 成为唯一验证入口（AudioWorklet 在浏览器外
没法跑，但 process() 是纯算术 + 索引，喂假渲染量子就能测）。没装 node 就跳过，
不让前端测试拖垮服务端 CI。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

SIM = Path(__file__).parent / "worklet_sim.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能跑 worklet 仿真")
def test_web_worklets_simulation():
    r = subprocess.run(["node", str(SIM)], capture_output=True, text=True, timeout=120)
    tail = "\n".join(r.stdout.strip().split("\n")[-20:])
    assert r.returncode == 0, f"worklet 仿真未通过：\n{tail}\n{r.stderr[-500:]}"
    assert "FAIL" not in r.stdout
