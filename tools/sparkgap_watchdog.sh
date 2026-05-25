#!/bin/bash
#
# sparkgap_watchdog.sh — kill+restart sparkgap.py if RSS > THRESHOLD_KB.
#
# Designed as a belt-and-suspenders during operator-busy periods (e.g.,
# WPX CW contest weekend) when the operator can't babysit the skimmer.
# Today's leak fix (MAX_ENV + malloc_trim + MALLOC_ARENA_MAX=2,
# commit 34dab6b) holds RSS in a 4.6-5.4 GB sawtooth band in normal
# operation, so 11 GB threshold gives plenty of headroom for slow
# drift while still catching anything pathological well before OOM
# (14 GB total RAM on skimmer1).
#
# Usage (on skimmer1):
#   nohup ./tools/sparkgap_watchdog.sh > /tmp/watchdog.log 2>&1 &
#   disown
#
# Or invoke once per minute via cron:
#   * * * * * /home/sparkgap/csdr-skimmer/tools/sparkgap_watchdog.sh --oneshot
#
# Stop:
#   pkill -f sparkgap_watchdog.sh
#
# Logs to /tmp/sparkgap_watchdog.log

set -u

THRESHOLD_KB=11534336        # 11 GB
CHECK_INTERVAL_SEC=300       # 5 min between checks
SPARKGAP_DIR=/home/sparkgap/csdr-skimmer
SPARKGAP_CONFIG=sk_5band.json
LOG=/tmp/sparkgap_watchdog.log
ONESHOT=0

[[ "${1:-}" == "--oneshot" ]] && ONESHOT=1

ts() { date '+%Y-%m-%d %H:%M:%S UTC'; }
log() { echo "$(ts) [watchdog] $*" >> "$LOG"; }

find_pid() {
    # Avoid pgrep -f substring trap (matches our own SSH/bash cmdline).
    # Walk /proc/*/cmdline directly.
    for p in /proc/[0-9]*; do
        local pid=${p##*/}
        [[ -r "$p/cmdline" ]] || continue
        local cmd
        cmd=$(tr "\0" " " < "$p/cmdline" 2>/dev/null)
        case "$cmd" in
            "python3 sparkgap.py --config $SPARKGAP_CONFIG"*)
                echo "$pid"
                return 0
                ;;
        esac
    done
    return 1
}

restart_sparkgap() {
    local pid=$1
    log "RESTART triggered: PID $pid RSS exceeded $THRESHOLD_KB KB"

    # Preserve the pre-restart log for post-mortem
    cp /tmp/sparkgap.log "/tmp/sparkgap.log.watchdog-$(date +%Y%m%d-%H%M%S)"
    log "  saved pre-restart log to /tmp/sparkgap.log.watchdog-*"

    # Polite kill first
    kill -TERM "$pid" 2>/dev/null
    for i in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || { log "  SIGTERM took ${i}s"; break; }
        sleep 1
    done
    # Force kill if still alive (sparkgap.py blocks SIGTERM in C dec_feed —
    # known issue, task #117).
    if kill -0 "$pid" 2>/dev/null; then
        log "  SIGTERM stuck after 20s, sending SIGKILL"
        kill -KILL "$pid" 2>/dev/null
        sleep 2
    fi

    # Restart with MALLOC_ARENA_MAX=2 (required for the glibc-arena fix
    # to take effect; see feedback_glibc_arena_fragmentation.md)
    cd "$SPARKGAP_DIR" || { log "  ERROR: cd $SPARKGAP_DIR failed"; return 1; }
    MALLOC_ARENA_MAX=2 nohup python3 sparkgap.py --config "$SPARKGAP_CONFIG" \
        > /tmp/sparkgap.log 2>&1 &
    local newpid=$!
    disown
    sleep 5
    if kill -0 "$newpid" 2>/dev/null; then
        log "  restarted as PID $newpid"
    else
        log "  ERROR: restart attempt produced dead PID $newpid"
    fi
}

check_once() {
    local pid
    pid=$(find_pid) || { log "no sparkgap.py process found"; return 0; }
    local rss_kb
    rss_kb=$(awk '/VmRSS:/{print $2}' "/proc/$pid/status" 2>/dev/null)
    [[ -z "$rss_kb" ]] && { log "PID $pid: no VmRSS readable"; return 0; }
    if (( rss_kb > THRESHOLD_KB )); then
        restart_sparkgap "$pid"
    fi
    # else: silent — only log threshold breaches
}

main() {
    log "watchdog start (threshold=$THRESHOLD_KB KB, interval=${CHECK_INTERVAL_SEC}s, oneshot=$ONESHOT)"
    if (( ONESHOT )); then
        check_once
        return
    fi
    while true; do
        check_once
        sleep "$CHECK_INTERVAL_SEC"
    done
}

main "$@"
