#!/bin/bash
# resume_loop.sh — retro-kr-patch FCC 세션 자동 재개
#
# 체크포인트에 in_progress 상태가 있으면 pi(DeepSeek)로 작업 재개.
# API 키 소진 시 5분 후 재시도. 모든 단계 완료 시 종료.
#
# cron 등록 (선택):
#   */10 * * * * /opt/retro-kr-patch-mcp/resume_loop.sh
#
# 수동 실행:
#   /opt/retro-kr-patch-mcp/resume_loop.sh <project_name>

set -euo pipefail

PROJECT="${1:-}"
CHECKPOINT_DIR="/root/projects/retro-kr-patch-mcp/checkpoints"
LOG_DIR="/root/projects/retro-kr-patch-mcp/logs"
MAX_RETRIES=10
RETRY_DELAY=300  # 5분

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_DIR/resume.log"
}

# ── 프로젝트 목록 확인 ──────────────────────────────────────────────────────
if [ -z "$PROJECT" ]; then
    # in_progress 상태인 첫 번째 프로젝트 찾기
    for f in "$CHECKPOINT_DIR"/*.json; do
        [ -f "$f" ] || continue
        PROJECT=$(basename "$f" .json)
        break
    done
fi

if [ -z "$PROJECT" ]; then
    log "체크포인트 없음. 대기 종료."
    exit 0
fi

CP_FILE="$CHECKPOINT_DIR/$PROJECT.json"
if [ ! -f "$CP_FILE" ]; then
    log "프로젝트 '$PROJECT' 체크포인트 없음."
    exit 0
fi

# ── 상태 확인 ────────────────────────────────────────────────────────────────
LAST_PHASE=$(python3 -c "
import json
with open('$CP_FILE') as f:
    d = json.load(f)
print(d.get('last_phase', 'done'))
" 2>/dev/null || echo "done")

RESUME_PROMPT=$(python3 -c "
import json
with open('$CP_FILE') as f:
    d = json.load(f)
print(d.get('resume_prompt', ''))
" 2>/dev/null || echo "")

log "프로젝트: $PROJECT | 마지막 단계: $LAST_PHASE"

# 모든 단계 완료 확인
if [ "$LAST_PHASE" = "done" ]; then
    log "모든 단계 완료. 종료."
    exit 0
fi

# ── API 키 가용성 확인 ──────────────────────────────────────────────────────
check_api_key() {
    # DeepSeek API 키 확인
    local keys_file="/mnt/synology_devdata/commander-ng/.env.master"
    if [ -f "$keys_file" ]; then
        # .env.master에서 DEEPSEEK_API_KEY 확인
        source "$keys_file" 2>/dev/null || true
        if [ -n "${DEEPSEEK_API_KEY:-}" ]; then
            return 0
        fi

        # fallback: pi config 확인
        if [ -f /root/.pi/config.json ]; then
            python3 -c "
import json
with open('/root/.pi/config.json') as f:
    c = json.load(f)
k = c.get('providers',{}).get('deepseek',{}).get('api_key','')
if k:
    exit(0)
exit(1)
" 2>/dev/null && return 0
        fi
    fi
    return 1
}

# ── pi 실행 ──────────────────────────────────────────────────────────────────
run_pi() {
    local prompt="$1"
    log "pi 실행: $prompt"

    # pi CLI로 작업 실행
    # --provider deepseek --model deepseek-v4-pro
    timeout 1800 pi --provider deepseek --model deepseek-v4-pro \
        "retro-kr-patch MCP 서버에 연결되어 있습니다. 체크포인트를 확인하세요. $prompt" \
        2>&1 | tee -a "$LOG_DIR/pi_${PROJECT}_$(date +%Y%m%d_%H%M%S).log"

    local exit_code=$?
    if [ $exit_code -eq 0 ]; then
        log "✅ pi 정상 완료"
        return 0
    elif [ $exit_code -eq 124 ]; then
        log "⏱ pi 타임아웃 (30분)"
        return 2
    else
        log "❌ pi 실패 (exit=$exit_code)"
        return 1
    fi
}

# ── 메인 루프 ────────────────────────────────────────────────────────────────
retry=0
while [ $retry -lt $MAX_RETRIES ]; do
    # API 키 확인
    if ! check_api_key; then
        retry=$((retry + 1))
        log "⏳ API 키 소진. ${RETRY_DELAY}초 후 재시도 ($retry/$MAX_RETRIES)"
        sleep "$RETRY_DELAY"
        continue
    fi

    # Resume prompt 구성
    if [ -n "$RESUME_PROMPT" ]; then
        prompt="[자동 재개] 프로젝트 '$PROJECT', 단계 '$LAST_PHASE'. $RESUME_PROMPT"
    else
        prompt="[자동 재개] 프로젝트 '$PROJECT', 단계 '$LAST_PHASE'. checkpoint_load('$PROJECT')로 상태 확인 후 계속 진행."
    fi

    # pi 실행
    run_pi "$prompt"
    result=$?

    if [ $result -eq 0 ]; then
        # 성공 — 상태 재확인
        NEW_PHASE=$(python3 -c "
import json
with open('$CP_FILE') as f:
    d = json.load(f)
print(d.get('last_phase', 'done'))
" 2>/dev/null || echo "done")

        if [ "$NEW_PHASE" = "done" ]; then
            log "🎉 프로젝트 '$PROJECT' 완료!"
            PushNotification --message "retro-kr-patch: $PROJECT 모든 단계 완료" 2>/dev/null || true
            exit 0
        fi
        LAST_PHASE="$NEW_PHASE"
        RESUME_PROMPT=$(python3 -c "
import json
with open('$CP_FILE') as f:
    d = json.load(f)
print(d.get('resume_prompt', ''))
" 2>/dev/null || echo "")
    elif [ $result -eq 2 ]; then
        # 타임아웃 — API 키 아직 살아있으면 계속
        log "타임아웃. 체크포인트 저장 확인 후 재시도."
    else
        # 실패 — API 키 소진으로 간주
        retry=$((retry + 1))
        log "실패. ${RETRY_DELAY}초 후 재시도 ($retry/$MAX_RETRIES)"
        sleep "$RETRY_DELAY"
    fi
done

log "최대 재시도 초과. 수동 개입 필요."
exit 1
