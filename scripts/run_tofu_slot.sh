#!/usr/bin/env bash
# 네온 두부 슬롯 러너
#   collect : 조간/석간 발행 전에 미리 수집해 원문을 보존 (오래 걸리는 단계)
#   publish : 정시에 요약 생성 + 사이트 발행 (짧게 끝남)
#   all     : 둘 다 (수동 테스트용)
#
# 사용: ./run_tofu_slot.sh <collect|publish|all> <morning|evening|auto> [추가 옵션...]
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

# 기존 Signal 러너(run_cron.sh)와 동일한 실행 환경을 쓴다 — 수집 메커니즘을 그대로 이식하기 위함
export PATH="/srv/first-light/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export CLOAKBROWSER_CACHE_DIR="${CLOAKBROWSER_CACHE_DIR:-/srv/first-light/cache/cloakbrowser}"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-/srv/first-light/cache/xdg-config}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-/srv/first-light/cache/xdg-cache}"
export XDG_DATA_HOME="${XDG_DATA_HOME:-/srv/first-light/cache/xdg-data}"
export HOME="${FIRST_LIGHT_HOME:-/srv/first-light/cache/home}"
mkdir -p "$CLOAKBROWSER_CACHE_DIR" "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$HOME"
export TOFU_PI_BIN="${TOFU_PI_BIN:-/srv/first-light/bin/pi}"
export GIT_TERMINAL_PROMPT=0
export GH_CONFIG_DIR="${GH_CONFIG_DIR:-/srv/first-light/secrets/gh}"

PY="${TOFU_PYTHON:-$DIR/.venv/bin/python}"
if [ ! -x "$PY" ]; then PY="$(command -v python3)"; fi

# 시크릿(디스코드 로그인, Gemini 키) — 기존 Signal 과 동일한 경로를 쓴다
DISCORD_CONFIG="${DISCORD_EXPORT_CONFIG:-/srv/first-light/secrets/discord_export_config.env}"
KEYS_CONFIG="${GEMINI_KEYS_CONFIG:-/srv/first-light/secrets/gemini_keys.env}"
export GEMINI_KEYS_CONFIG="$KEYS_CONFIG"

PHASE="${1:-all}"
SLOT="${2:-auto}"
shift 2 2>/dev/null || true

if [ -f "$DISCORD_CONFIG" ]; then
  set -a
  . <(grep -E '^(DISCORD_EMAIL|DISCORD_PASSWORD|DISCORD_STORAGE_STATE)=' "$DISCORD_CONFIG") || true
  set +a
fi

exec "$PY" pipeline/run_tofu.py --phase "$PHASE" --slot "$SLOT" "$@"
