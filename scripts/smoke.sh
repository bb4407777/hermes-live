#!/bin/bash
# Hermes-live 冒烟测试。全程 8647 LLM 调用 ≤2 次；绝不重启任何服务。
set -uo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
FAIL=0

step() { printf '\n== %s ==\n' "$1"; }

step "1/4 Hermes gateway 健康（不耗 token）"
curl -sf -H "Authorization: Bearer hermes-local-key" http://127.0.0.1:8647/v1/health \
  && echo " OK" || { echo "FAIL：8647 不可达"; FAIL=1; }

step "2/4 hermes-live 服务健康（未启动则跳过）"
if curl -sf http://127.0.0.1:8698/api/health 2>/dev/null; then echo " OK"
else echo "（8698 未启动，跳过——起服务：$PY -m server.main）"; fi

step "3/4 edge-tts 独立合成（0 次 LLM）"
$PY scripts/make_test_wav.py "冒烟测试。" >/dev/null \
  && echo "OK" || { echo "FAIL：edge-tts 不可用"; FAIL=1; }

step "4/4 全链路一次（恰 1 次 LLM 调用：wav→VAD→ASR→Hermes→TTS）"
$PY scripts/make_test_wav.py "请只回答收到两个字。" >/dev/null || FAIL=1
$PY -m cli.m0_pipeline --wav tmp/test16k.wav --out tmp/reply.wav \
  && echo "OK" || { echo "FAIL：全链路未通过"; FAIL=1; }

printf '\n== 冒烟%s ==\n' "$([ $FAIL -eq 0 ] && echo 通过 || echo 失败)"
exit $FAIL
