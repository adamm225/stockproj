#!/usr/bin/env bash
# setup_schedule.sh
# Adds (or removes) 5 cron entries that fire at 6:30-10:30 AM Eastern every day.
# TZ=America/New_York is set in the crontab block so it works regardless of
# the server's local timezone — no manual time conversion needed.
#
# Usage:
#   chmod +x setup_schedule.sh
#   ./setup_schedule.sh          # install
#   ./setup_schedule.sh --remove # uninstall

PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$PROJ_DIR/run_scanner.sh"
MARKER="# StockScanner"

if [ ! -x "$SCRIPT" ]; then
    chmod +x "$SCRIPT"
fi

remove_entries() {
    crontab -l 2>/dev/null | grep -v "$MARKER" | crontab -
    echo "Removed StockScanner cron entries."
}

install_entries() {
    # Strip any existing entries first so re-running is idempotent
    EXISTING=$(crontab -l 2>/dev/null | grep -v "$MARKER" || true)

    NEW_ENTRIES=$(cat <<EOF

TZ=America/New_York $MARKER
30  6 * * * "$SCRIPT" $MARKER 06:30
30  7 * * * "$SCRIPT" $MARKER 07:30
30  8 * * * "$SCRIPT" $MARKER 08:30
30  9 * * * "$SCRIPT" $MARKER 09:30
30 10 * * * "$SCRIPT" $MARKER 10:30
EOF
)

    printf '%s\n%s\n' "$EXISTING" "$NEW_ENTRIES" | crontab -
    echo "Installed 5 cron entries (6:30–10:30 AM ET daily)."
    echo ""
    echo "Current crontab:"
    crontab -l
}

if [ "${1:-}" = "--remove" ]; then
    remove_entries
else
    install_entries
fi
