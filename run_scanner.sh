#!/usr/bin/env bash
# run_scanner.sh — invoked by cron
# 6:30 / 7:30 / 8:30 / 9:30 ET  →  --morning  (Strategies 1, 2, 3)
# 10:30 ET                       →  --ten-am   (Strategy 7 + morning set)

set -euo pipefail

PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="$PROJ_DIR/.venv/bin/python"
RUNS_DIR="$PROJ_DIR/runs"
LOG="$PROJ_DIR/ScannerRun.txt"

mkdir -p "$RUNS_DIR"

# Pick flag based on current ET hour
HOUR=$(TZ="America/New_York" date +%-H)
if [ "$HOUR" -ge 10 ]; then
    FLAG="--ten-am"
else
    FLAG="--morning"
fi

STAMP=$(TZ="America/New_York" date "+%Y-%m-%d %H:%M ET")

# Use venv python if available, fall back to system python3
if [ -x "$VENV_PYTHON" ]; then
    PYTHON="$VENV_PYTHON"
else
    PYTHON="python3"
fi

# Run scanner, capture output
TMP="$RUNS_DIR/_last_run.txt"
cd "$PROJ_DIR"
"$PYTHON" nasdaq_v3.py $FLAG --export 2>&1 | tee "$TMP"

# Move any newly-produced strategy CSVs into runs/
find "$PROJ_DIR" -maxdepth 1 -name 's*_*.csv' | while read -r f; do
    mv "$f" "$RUNS_DIR/"
done

# Append to ScannerRun.txt
{
    printf '\n\n========================================================================\n'
    printf '  SCAN @ %s   (flag: %s)\n' "$STAMP" "$FLAG"
    printf '========================================================================\n'
    cat "$TMP"
} >> "$LOG"

rm -f "$TMP"
