#!/bin/bash
# Starts the tester UI and acts on its exit code:
#   0  -> power off          10 -> reboot (asked for from the menu)
#   20 -> already running    30 -> exit to desktop
#   anything else -> crash -> reboot
# Safety nets:
#   * touch ~/pitester/MAINTENANCE  -> any exit just drops you to the desktop
#   * 3 crashes within 10 minutes   -> stops rebooting (no boot loop)

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$APP_DIR/logs"
CRASH_FILE="$APP_DIR/.crashes"
MAINT_FLAG="$APP_DIR/MAINTENANCE"
MAX_CRASHES=3
WINDOW=600

mkdir -p "$LOG_DIR"
log() { echo "$(date '+%F %T') $*" >> "$LOG_DIR/launcher.log"; }

sleep 3   # let the desktop, touch and network settle

cd "$APP_DIR" || exit 1
python3 "$APP_DIR/app.py" >> "$LOG_DIR/stdout.log" 2>&1
rc=$?
log "app exited with $rc"

# system already shutting down / rebooting? stay out of the way
state="$(systemctl is-system-running 2>/dev/null)"
[ "$state" = "stopping" ] && exit 0

if [ -e "$MAINT_FLAG" ]; then
    log "MAINTENANCE flag present - staying on desktop"
    exit 0
fi

case $rc in
    0)  rm -f "$CRASH_FILE"; log "powering off"; sudo -n systemctl poweroff ;;
    10) rm -f "$CRASH_FILE"; log "rebooting (requested)"; sudo -n systemctl reboot ;;
    20) log "already running - nothing to do" ;;
    30) rm -f "$CRASH_FILE"; log "exit to desktop requested" ;;
    *)
        now=$(date +%s)
        echo "$now" >> "$CRASH_FILE"
        recent=$(awk -v n="$now" -v w="$WINDOW" '$1 > n - w' "$CRASH_FILE" | wc -l)
        if [ "$recent" -ge "$MAX_CRASHES" ]; then
            log "crash loop ($recent crashes in ${WINDOW}s) - NOT rebooting, see logs"
            exit 1
        fi
        log "crash - rebooting"
        sudo -n systemctl reboot
        ;;
esac
