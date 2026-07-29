#!/usr/bin/env bash
# Clone the repo, run this, done.
#
#   git clone <repo> && cd RaspberryImagingAPI && ./setup.sh
#
# Idempotent: safe to re-run after a pull. Everything it touches is either
# checked first or overwritten with the same content.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="${SUDO_USER:-$(id -un)}"
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
PORT=8080
SERVICE=imaging-station

if [ "$(id -u)" -eq 0 ] && [ -z "${SUDO_USER:-}" ]; then
  echo "Run this as your normal user, not as root. It will sudo when it needs to." >&2
  exit 1
fi

say() { printf '\n\033[1;34m==\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------- packages --
say "Installing system packages"
sudo apt-get update -qq
# avahi is what makes http://$(hostname).local work from a laptop on the far
# end of a plain ethernet cable, with no router and no DHCP server.
sudo apt-get install -y -qq \
  python3-venv python3-pip python3-dev \
  avahi-daemon curl ca-certificates
sudo systemctl enable --now avahi-daemon >/dev/null 2>&1 || true

CHROMIUM=""
for c in chromium-browser chromium; do
  command -v "$c" >/dev/null 2>&1 && { CHROMIUM="$c"; break; }
done
if [ -z "$CHROMIUM" ]; then
  sudo apt-get install -y -qq chromium || sudo apt-get install -y -qq chromium-browser || true
  for c in chromium-browser chromium; do
    command -v "$c" >/dev/null 2>&1 && { CHROMIUM="$c"; break; }
  done
fi

# ------------------------------------------------------------------ python --
say "Building the virtualenv"
# --system-site-packages on purpose: FLIR's Spinnaker installer drops PySpin
# into /usr/lib/python3/dist-packages, and an isolated venv cannot see it.
# Without this flag the station silently runs on synthetic frames forever.
# A venv built without that flag can never see PySpin, and re-running would
# happily reuse it. Detect and rebuild instead of leaving a permanent stub.
if [ -d "$DIR/.venv" ] &&
   ! grep -q 'include-system-site-packages *= *true' "$DIR/.venv/pyvenv.cfg" 2>/dev/null; then
  warn "Existing venv is isolated from system packages; rebuilding it."
  rm -rf "$DIR/.venv"
fi
[ -d "$DIR/.venv" ] || python3 -m venv --system-site-packages "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

# --------------------------------------------------------------- spinnaker --
# The SDK cannot ship in this repo: it is proprietary Teledyne FLIR software
# and its licence forbids redistribution. Drop the two tarballs you downloaded
# from FLIR into vendor/ and this installs them unattended. vendor/ is
# gitignored so the SDK can never be committed by accident.
install_spinnaker() {
  local pkg py whl tmp pyver
  pkg="$(ls "$DIR"/vendor/spinnaker-*-arm64-pkg*.tar.gz 2>/dev/null | head -1 || true)"
  py="$(ls "$DIR"/vendor/spinnaker_python-*aarch64*.tar.gz 2>/dev/null | head -1 || true)"
  [ -n "$pkg" ] || return 1

  say "Installing the Spinnaker SDK from vendor/"
  tmp="$(mktemp -d)"
  tar -xzf "$pkg" -C "$tmp"

  sudo apt-get install -y -qq \
    libusb-1.0-0 libgomp1 libavcodec-dev libavformat-dev libswscale-dev \
    libswresample-dev libavutil-dev 2>/dev/null || true

  # Their .debs prompt about udev rules and the usbfs limit. Noninteractive
  # takes the defaults; the group and buffer are set explicitly below anyway.
  ( cd "$(dirname "$(find "$tmp" -name 'libspinnaker*.deb' | head -1)")" &&
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ./*.deb )

  # Camera access without root. Without this PySpin enumerates zero devices
  # when the station runs as a service, which looks exactly like a dead camera.
  sudo groupadd -f flirimaging
  sudo usermod -aG flirimaging "$USER_NAME"
  NEED_REBOOT=1

  if [ -n "$py" ]; then
    tar -xzf "$py" -C "$tmp"
    pyver="$("$DIR/.venv/bin/python" -c 'import sys;print(f"cp{sys.version_info.major}{sys.version_info.minor}")')"
    whl="$(find "$tmp" -name "*$pyver*aarch64.whl" | head -1 || true)"
    if [ -n "$whl" ]; then
      "$DIR/.venv/bin/pip" install -q "$whl"
    else
      warn "No PySpin wheel for $pyver in $(basename "$py")."
      warn "Available: $(find "$tmp" -name '*.whl' -printf '%f ' 2>/dev/null)"
      warn "Download the spinnaker_python build matching $pyver."
    fi
  else
    warn "Found the SDK but no spinnaker_python-*aarch64.tar.gz in vendor/."
  fi
  rm -rf "$tmp"
}

if "$DIR/.venv/bin/python" -c "import PySpin" 2>/dev/null; then
  echo "   PySpin found -- the Blackfly will be used."
elif install_spinnaker && "$DIR/.venv/bin/python" -c "import PySpin" 2>/dev/null; then
  echo "   Spinnaker installed; PySpin imports."
else
  warn "PySpin not installed -- the station will run on synthetic frames."
  warn "The SDK is not on PyPI and cannot be redistributed here. Download the"
  warn "ARM64 build from flir.com/products/spinnaker-sdk (free account), put"
  warn "both tarballs in $DIR/vendor/ and re-run this script:"
  warn "    spinnaker-<ver>-arm64-pkg.tar.gz"
  warn "    spinnaker_python-<ver>-cp<XY>-cp<XY>-linux_aarch64.tar.gz"
fi

# -------------------------------------------------------------- usb buffer --
# The default 16 MB usbfs buffer drops frames from a 5 MP camera; you get
# img.IsIncomplete() rather than an error, so it looks like flaky hardware.
CMDLINE=/boot/firmware/cmdline.txt
[ -f "$CMDLINE" ] || CMDLINE=/boot/cmdline.txt
if [ -f "$CMDLINE" ]; then
  if grep -q usbcore.usbfs_memory_mb "$CMDLINE"; then
    echo "   usbfs buffer already set."
  else
    say "Raising the USB buffer to 1000 MB (needs a reboot)"
    sudo cp "$CMDLINE" "$CMDLINE.bak"
    sudo sed -i '1s/$/ usbcore.usbfs_memory_mb=1000/' "$CMDLINE"
    NEED_REBOOT=1
  fi
else
  warn "No cmdline.txt found; skipping the USB buffer tweak."
fi

# ------------------------------------------------------------ pico serial --
if ! id -nG "$USER_NAME" | grep -qw dialout; then
  say "Adding $USER_NAME to dialout (for the Pico's serial port)"
  sudo usermod -aG dialout "$USER_NAME"
  NEED_REBOOT=1
fi

# ------------------------------------------------------------------ service --
say "Installing the $SERVICE service"
sudo tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<UNIT
[Unit]
Description=Wood grading imaging station
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python $DIR/station.py
Restart=always
RestartSec=5
# Images live in the spool, never in the journal. Capped so a crash loop
# cannot fill the SD card with tracebacks.
StandardOutput=journal
StandardError=journal
LogRateLimitIntervalSec=30
LogRateLimitBurst=200

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null
sudo systemctl restart "$SERVICE"

# -------------------------------------------------------------------- kiosk --
# A desktop autostart entry rather than a systemd unit: the desktop session
# owns the display, so this works under both X11 and Wayland without guessing
# at DISPLAY or WAYLAND_DISPLAY.
if [ -n "$CHROMIUM" ]; then
  say "Setting up the kiosk browser ($CHROMIUM)"
  chmod +x "$DIR/open-ui.sh"

  # Everything below launches open-ui.sh rather than repeating the browser
  # invocation: one copy to change, and the wait-for-the-station guard and the
  # already-open check come along for free.
  write_launcher() {   # $1 = path, $2 = Name, $3 = extra lines
    cat > "$1" <<LAUNCHER
[Desktop Entry]
Type=Application
Name=$2
Comment=Open the wood grading station UI full screen
Icon=camera-photo
Terminal=false
Categories=Utility;
Exec=$DIR/open-ui.sh
$3
LAUNCHER
    chmod +x "$1"
  }

  mkdir -p "$USER_HOME/.config/autostart"
  write_launcher "$USER_HOME/.config/autostart/imaging-kiosk.desktop" \
                 "Imaging station kiosk" "X-GNOME-Autostart-enabled=true"

  # A way back in. Closing the kiosk -- Alt+F4, or Settings -> Exit full screen
  # -- otherwise leaves no route to the UI without a terminal, because the
  # autostart entry only fires at login.
  mkdir -p "$USER_HOME/.local/share/applications"
  write_launcher "$USER_HOME/.local/share/applications/imaging-station.desktop" \
                 "Imaging Station" ""

  # And on the desktop itself, so getting back is a double-click.
  DESKTOP_DIR="$USER_HOME/Desktop"
  [ -d "$DESKTOP_DIR" ] &&
    write_launcher "$DESKTOP_DIR/imaging-station.desktop" "Imaging Station" ""

  # Only needed if someone ran the whole script under sudo.
  [ "$(id -u)" -eq 0 ] &&
    chown -R "$USER_NAME:$USER_NAME" "$USER_HOME/.config/autostart" \
      "$USER_HOME/.local/share/applications" "$DESKTOP_DIR" 2>/dev/null
  # A grading station that blanks mid-shift looks broken to the operator.
  command -v raspi-config >/dev/null 2>&1 && sudo raspi-config nonint do_blanking 1 || true
else
  warn "No chromium found; skipping the kiosk. The web UI still works."
fi

# ------------------------------------------------------------------- report --
sleep 2
if systemctl is-active --quiet "$SERVICE"; then
  STATE="running"
else
  STATE="NOT running -- check: journalctl -u $SERVICE -n 40"
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<REPORT

  Station is $STATE.

    on the touchscreen   http://localhost:$PORT
    from a laptop        http://$(hostname).local:$PORT${IP:+  or  http://$IP:$PORT}

  Set the Jetson URL and ingest key in the Settings tab.

    logs      journalctl -u $SERVICE -f
    restart   sudo systemctl restart $SERVICE
    tests     $DIR/.venv/bin/python $DIR/test_station.py

REPORT

if [ "${NEED_REBOOT:-0}" = "1" ]; then
  warn "Reboot before the first real capture: the USB buffer and the dialout"
  warn "group only take effect after one.   sudo reboot"
fi
