#!/usr/bin/env bash
# Open the station UI full screen.
#
#   ./open-ui.sh            kiosk, what the desktop icon and autostart run
#   ./open-ui.sh --window   a normal window, easier when you are debugging
#
# The single place the browser invocation lives: the login autostart entry and
# the desktop launcher both call this, so there is one copy to change.
set -euo pipefail

PORT="${PORT:-8080}"
URL="http://localhost:$PORT"
SERVICE=imaging-station

MODE="--kiosk"
[ "${1:-}" = "--window" ] && MODE=""

# The station is a separate service; the browser closing never stops it. If it
# genuinely is not running, say so rather than spinning for a minute.
if command -v systemctl >/dev/null 2>&1 &&
   ! systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
  echo "The $SERVICE service is not active. Start it with:" >&2
  echo "    sudo systemctl start $SERVICE" >&2
  echo "Waiting anyway in case it is still coming up..." >&2
fi

# Wait for it to answer, so a double-click cannot land on an error page. Say
# so first -- a silent minute looks like the script did nothing.
if ! curl -sf "$URL/api/health" >/dev/null 2>&1; then
  echo "Waiting for the station on $URL ..."
  for _ in $(seq 45); do
    sleep 1
    curl -sf "$URL/api/health" >/dev/null 2>&1 && break
  done
fi

if ! curl -sf "$URL/api/health" >/dev/null 2>&1; then
  echo "The station is not answering on $URL." >&2
  echo "    journalctl -u $SERVICE -n 40 --no-pager" >&2
  exit 1
fi

# Over SSH there is no display to open anything on. Print where to go instead
# of failing with a Chromium error nobody reads.
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  echo "Station is up, but there is no display on this session. Reach it at:"
  echo "    $URL                       (on the Pi itself)"
  echo "    http://$(hostname).local:$PORT   (from another machine)"
  exit 0
fi

# Do not stack a second browser on top of one already showing the UI.
if pgrep -f "chromium.*--app=$URL" >/dev/null 2>&1; then
  echo "The UI is already open."
  exit 0
fi

BROWSER=""
for c in chromium chromium-browser; do
  if command -v "$c" >/dev/null 2>&1; then BROWSER="$c"; break; fi
done
if [ -z "$BROWSER" ]; then
  echo "No chromium found. Open $URL in any browser." >&2
  exit 1
fi

# shellcheck disable=SC2086  # MODE is deliberately word-split (empty or --kiosk)
exec "$BROWSER" $MODE --app="$URL" \
  --window-size=800,480 --window-position=0,0 \
  --noerrdialogs --disable-infobars --disable-session-crashed-bubble \
  --disable-pinch --overscroll-history-navigation=0 \
  --check-for-update-interval=31536000
