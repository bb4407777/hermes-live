#!/bin/bash
# Hermes-live 外部重启脚本
# 参考 areco 的重启模式，从服务外通过 kill + supervisor 拉起实现重启
# 用途：小程序重启按钮、手动运维、故障恢复

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="${PROJECT_ROOT}/logs/restart.log"
mkdir -p "$(dirname "$LOG_FILE")"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" | tee -a "$LOG_FILE"; }

# 查找 hermes-live 进程（匹配 python -m server.main）
find_pid() {
    pgrep -f "python.*server\.main" || true
}

# 优雅停止（SIGTERM，等待退出）
graceful_stop() {
    local pid="$1"
    local timeout="${2:-10}"

    log "发送 SIGTERM 到 PID $pid，等待最多 ${timeout}s..."
    if ! kill -TERM "$pid" 2>/dev/null; then
        log "WARN: 进程 $pid 已不存在"
        return 1
    fi

    local waited=0
    while kill -0 "$pid" 2>/dev/null; do
        if [[ $waited -ge $timeout ]]; then
            log "WARN: 等待 ${timeout}s 超时，强制 SIGKILL"
            kill -KILL "$pid" 2>/dev/null || true
            sleep 1
            return 0
        fi
        sleep 0.5
        waited=$((waited + 1))
    done

    log "进程 $pid 已退出"
    return 0
}

# 等待服务恢复（health check）
wait_for_service() {
    local port="${1:-8698}"
    local timeout="${2:-30}"
    local waited=0

    log "等待服务在端口 $port 恢复..."
    while ! curl -sf "http://127.0.0.1:${port}/api/health" >/dev/null 2>&1; do
        if [[ $waited -ge $timeout ]]; then
            log "ERROR: 服务 ${timeout}s 未恢复"
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done

    log "✓ 服务已恢复（${waited}s）"
    return 0
}

LAUNCHD_LABEL="com.gaochengbin.hermes-live"

main() {
    log "==== Hermes-live 重启开始 ===="

    # launchd 接管时必须走 kickstart：直接 kill 会被 KeepAlive 自动拉起，
    # 下面的 nohup 再起一个就双进程了
    if launchctl print "gui/$(id -u)/${LAUNCHD_LABEL}" >/dev/null 2>&1; then
        log "launchd 模式：kickstart -k ${LAUNCHD_LABEL}"
        launchctl kickstart -k "gui/$(id -u)/${LAUNCHD_LABEL}"
        if wait_for_service 8698 30; then
            log "==== 重启成功（launchd）===="
            exit 0
        else
            log "==== 重启失败：服务未恢复（launchd）===="
            exit 1
        fi
    fi

    # 1. 查找现有进程
    local old_pid
    old_pid=$(find_pid)

    if [[ -z "$old_pid" ]]; then
        log "WARN: 未找到运行中的 hermes-live 进程"
        log "尝试启动服务..."
        cd "$PROJECT_ROOT"
        nohup .venv/bin/python -m server.main >> logs/service.log 2>&1 &
        local new_pid=$!
        log "启动新进程 PID $new_pid"

        if wait_for_service 8698 30; then
            log "==== 重启成功（冷启动）===="
            exit 0
        else
            log "==== 重启失败：服务未恢复 ===="
            exit 1
        fi
    fi

    log "找到进程 PID $old_pid"

    # 2. 优雅停止
    if ! graceful_stop "$old_pid" 10; then
        log "WARN: 停止进程失败，继续尝试启动"
    fi

    # 3. 启动新进程
    log "启动新进程..."
    cd "$PROJECT_ROOT"
    nohup .venv/bin/python -m server.main >> logs/service.log 2>&1 &
    local new_pid=$!
    log "启动新进程 PID $new_pid"

    # 4. 等待服务恢复
    if wait_for_service 8698 30; then
        log "==== 重启成功 ===="
        exit 0
    else
        log "==== 重启失败：服务未恢复 ===="
        # 尝试读取最后几行错误日志
        if [[ -f logs/service.log ]]; then
            log "最后 10 行日志："
            tail -10 logs/service.log | tee -a "$LOG_FILE"
        fi
        exit 1
    fi
}

main "$@"
