#!/usr/bin/env bash
# Run this when the station is being confusing.
#
#   ./fix.sh
#
# Finds every copy of the station on this Pi, works out which one is real,
# points the service at it, frees the port if something else grabbed it,
# updates, restarts, then proves the page being served is the page on disk.
#
# Written because two checkouts and a stray process can leave you looking at
# a stale UI while git, the service and the browser all report success.
set -euo pipefail

SERVICE=imaging-station
PORT=8080
say()  { printf '\n\033[1;34m==\033[0m %s\n' "$*"; }
ok()   { printf '   \033[1;32m+\033[0m %s\n' "$*"; }
warn() { printf '   \033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------- find every copy --
say "Looking for copies of the station"
declare -a DIRS=()
add_dir() {
  local d
  d="$(cd "$1" 2>/dev/null && pwd -P)" || return 0
  [ -f "$d/station.py" ] || return 0
  local seen
  for seen in ${DIRS[@]+"${DIRS[@]}"}; do [ "$seen" = "$d" ] && return 0; done
  DIRS+=("$d")
}

add_dir "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
svc_dir="$(systemctl show -p WorkingDirectory --value "$SERVICE" 2>/dev/null || true)"
[ -n "$svc_dir" ] && add_dir "$svc_dir"
while IFS= read -r f; do
  add_dir "$(dirname "$f")"
done < <(find "$HOME" -maxdepth 4 -name station.py -not -path '*/.venv/*' 2>/dev/null)

[ ${#DIRS[@]} -gt 0 ] || die "No copy of station.py found under $HOME."

has_pyspin() { "$1/.venv/bin/python" -c 'import PySpin' 2>/dev/null; }
commit_at()  { git -C "$1" log -1 --format=%ct 2>/dev/null || echo 0; }

for d in "${DIRS[@]}"; do
  printf '   %s\n      commit %s   PySpin %s\n' \
    "$d" \
    "$(git -C "$d" log --oneline -1 2>/dev/null || echo 'not a git repo')" \
    "$(has_pyspin "$d" && echo yes || echo no)"
done

# ------------------------------------------------------------ pick one --
# A working PySpin wins outright: rebuilding it costs half an hour, whereas
# a stale checkout is one git pull away. Newest commit breaks the tie.
declare -a POOL=()
for d in "${DIRS[@]}"; do has_pyspin "$d" && POOL+=("$d"); done
if [ ${#POOL[@]} -eq 0 ]; then
  POOL=("${DIRS[@]}")
  [ ${#DIRS[@]} -gt 1 ] && warn "No copy has a working PySpin; going by commit date."
fi

KEEP="${POOL[0]}"
for d in ${POOL[@]+"${POOL[@]}"}; do
  [ "$(commit_at "$d")" -gt "$(commit_at "$KEEP")" ] && KEEP="$d"
done
say "Using $KEEP"
[ ${#DIRS[@]} -gt 1 ] && warn "Other copies exist. Pull in $KEEP, not them."

# ------------------------------------------------- carry settings across --
if [ ! -f "$KEEP/config.json" ]; then
  for d in "${DIRS[@]}"; do
    if [ "$d" != "$KEEP" ] && [ -f "$d/config.json" ]; then
      cp "$d/config.json" "$KEEP/config.json"
      ok "Copied config.json (Jetson URL and key) from $d"
      break
    fi
  done
fi

# ------------------------------------------------------------- update --
if git -C "$KEEP" rev-parse --git-dir >/dev/null 2>&1; then
  say "Updating"
  if git -C "$KEEP" pull --ff-only 2>&1 | sed 's/^/   /'; then :; else
    warn "git pull failed -- local edits? The rest of this still runs."
  fi
fi
ok "at $(git -C "$KEEP" log --oneline -1 2>/dev/null || echo 'no git')"

# ------------------------------------------------------- free the port --
# A hand-started "python station.py" keeps the port, so systemctl restart
# fails to bind and the OLD process goes on serving the OLD page -- while
# every command you ran reported success.
say "Freeing port $PORT"
sudo systemctl stop "$SERVICE" 2>/dev/null || true
sleep 1
strays="$(pgrep -f '[s]tation\.py' || true)"
if [ -n "$strays" ]; then
  warn "Killing stray station.py: $(echo "$strays" | tr '\n' ' ')"
  # shellcheck disable=SC2086
  kill $strays 2>/dev/null || true
  sleep 2
  # shellcheck disable=SC2086
  kill -9 $strays 2>/dev/null || true
fi
ok "port clear"

# --------------------------------------------- reinstall the unit + start --
say "Pointing the service at $KEEP"
bash "$KEEP/setup.sh" 2>&1 | sed 's/^/   /' || die "setup.sh failed; read the output above."

# ------------------------------------------------------------- verify --
say "Checking the page being served is the page on disk"
for _ in $(seq 30); do
  curl -sf "http://localhost:$PORT/api/health" >/dev/null 2>&1 && break
  sleep 1
done
served="$(curl -s "http://localhost:$PORT/" | md5sum | cut -d' ' -f1)"
ondisk="$(md5sum "$KEEP/static/index.html" | cut -d' ' -f1)"
if [ "$served" = "$ondisk" ]; then
  ok "served page matches $KEEP/static/index.html"
else
  die "The server is still serving a different page than $KEEP.
    served $served   on disk $ondisk
    Something else is on port $PORT. Look at:  sudo ss -lptn 'sport = :$PORT'"
fi

# ------------------------------------------------------------ browser --
if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  say "Reopening the UI"
  bash "$KEEP/open-ui.sh" --force >/dev/null 2>&1 &
  ok "browser reopening"
fi

cam="$(curl -s "http://localhost:$PORT/api/health" |
       tr ',' '\n' | grep -m1 '"name"' | cut -d'"' -f4 || true)"
cat <<DONE

  Fixed.

    directory   $KEEP
    camera      ${cam:-unknown}
    open at     http://localhost:$PORT
                http://$(hostname).local:$PORT

  From now on, pull in $KEEP.
  Settings shows a UI build stamp -- check it there if a change seems missing.

DONE
