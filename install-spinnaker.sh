#!/usr/bin/env bash
# Install Spinnaker + PySpin without apt.
#
#   ./install-spinnaker.sh
#
# Why not apt: FLIR build their ARM64 debs against Ubuntu 20.04/22.04, and
# Raspberry Pi OS is Debian. Bookworm has libavcodec59 where those packages
# demand libavcodec58, so apt hits an unmet dependency it can never satisfy
# and leaves half-configured packages behind.
#
# dpkg -x unpacks a deb's FILES only -- no dependency resolution, no
# maintainer scripts, nothing written to apt's state. Spinnaker is shared
# libraries plus a udev rule; the dependency metadata is what fights you, not
# the software. So unpack the libraries, put them somewhere ldconfig can see,
# and install the wheel against them.
#
# Idempotent. Re-run it after a failed attempt.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR="$DIR/vendor"
PREFIX=/opt/spinnaker
VENV="$DIR/.venv"
USER_NAME="${SUDO_USER:-$(id -un)}"

say()  { printf '\n\033[1;34m==\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------ checks --
arch="$(dpkg --print-architecture 2>/dev/null || echo unknown)"
[ "$arch" = "arm64" ] || die "This is $arch ($(uname -m)); the vendor/ tarballs
are arm64. Flash 64-bit Raspberry Pi OS, or fetch the matching FLIR build."

pkg="$(ls "$VENDOR"/spinnaker-*arm64*.tar.gz 2>/dev/null | head -1 || true)"
py="$(ls "$VENDOR"/spinnaker_python-*aarch64*.tar.gz 2>/dev/null | head -1 || true)"
[ -n "$pkg" ] || die "No spinnaker-*arm64*.tar.gz in $VENDOR. See vendor/README.md"
[ -d "$VENV" ] || die "No virtualenv yet. Run ./setup.sh first."

# ------------------------------------------------ clean up a failed attempt --
if dpkg -l 2>/dev/null | grep -qE '^i[^ ]*\s+(libspinnaker|libgentl|spinview|spinupdate)'; then
  say "Removing half-installed Spinnaker packages from the apt attempt"
  sudo apt-get remove --purge -y 'libspinnaker*' 'libgentl*' 'spinview*' \
    'spinupdate*' 'libspinvideo*' 2>/dev/null || true
  sudo apt-get --fix-broken install -y || true
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# ------------------------------------------------------------- unpack libs --
say "Unpacking $(basename "$pkg")"
tar -xzf "$pkg" -C "$tmp"

stage="$tmp/stage"
mkdir -p "$stage"
count=0
# SpinView is the GUI; it drags in Qt and this station never opens it.
while IFS= read -r -d '' deb; do
  case "$(basename "$deb")" in
    *spinview*|*doc*|*-dev_*) continue ;;
  esac
  dpkg -x "$deb" "$stage"
  count=$((count + 1))
done < <(find "$tmp" -name '*.deb' -print0)
[ "$count" -gt 0 ] || die "No .deb files inside $(basename "$pkg")."
echo "   unpacked $count packages, no apt involved"

say "Installing libraries to $PREFIX"
sudo mkdir -p "$PREFIX/lib"
while IFS= read -r -d '' so; do
  sudo cp -a "$so" "$PREFIX/lib/"
done < <(find "$stage" -name '*.so*' -print0)

# ldconfig, not LD_LIBRARY_PATH: the station runs as a systemd service and
# would not inherit a shell variable.
echo "$PREFIX/lib" | sudo tee /etc/ld.so.conf.d/spinnaker.conf >/dev/null
sudo ldconfig

# ------------------------------------------------------- udev + permissions --
say "Camera permissions"
found_rules=0
while IFS= read -r -d '' rule; do
  sudo cp "$rule" /etc/udev/rules.d/
  found_rules=1
done < <(find "$stage" -path '*udev/rules.d/*' -name '*.rules' -print0)

if [ "$found_rules" = "0" ]; then
  # FLIR's own rule, written out directly if the deb did not carry one.
  # Without it the camera is root-only and PySpin enumerates zero devices,
  # which looks exactly like a dead camera.
  echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="1e10", GROUP="flirimaging", MODE="0664"' |
    sudo tee /etc/udev/rules.d/40-flir-spinnaker.rules >/dev/null
fi
sudo groupadd -f flirimaging
sudo usermod -aG flirimaging "$USER_NAME"
sudo udevadm control --reload-rules && sudo udevadm trigger || true

# ------------------------------------------------------ what is still missing --
main_so="$(find "$PREFIX/lib" -name 'libSpinnaker.so*' | head -1 || true)"
if [ -n "$main_so" ]; then
  missing="$(ldd "$main_so" 2>/dev/null | awk '/not found/{print $1}' | sort -u)"
  if [ -n "$missing" ]; then
    warn "libSpinnaker still wants:"
    printf '     %s\n' $missing
    warn "Most of these are ffmpeg libs only SpinVideo needs; capture usually"
    warn "works without them. If import fails, try:"
    warn "    sudo apt install libavcodec59 libavformat59 libswscale6 libusb-1.0-0"
  else
    echo "   all shared libraries resolve"
  fi
fi

# ------------------------------------------------------------------ PySpin --
if [ -z "$py" ]; then
  warn "No spinnaker_python-*aarch64*.tar.gz in $VENDOR -- libraries are in,"
  warn "but PySpin itself is not. Download it and re-run."
  exit 0
fi

say "Installing PySpin"
tar -xzf "$py" -C "$tmp"
pyver="$("$VENV/bin/python" -c 'import sys;print(f"cp{sys.version_info.major}{sys.version_info.minor}")')"
whl="$(find "$tmp" -name "*${pyver}*aarch64.whl" | head -1 || true)"
if [ -z "$whl" ]; then
  warn "No wheel for $pyver in $(basename "$py"). It contains:"
  find "$tmp" -name '*.whl' -printf '     %f\n'
  die "Download the spinnaker_python build matching $pyver."
fi
"$VENV/bin/pip" install --force-reinstall "$whl"

# --------------------------------------------------------------- verify it --
say "Verifying"
if "$VENV/bin/python" - <<'PY'
import sys
try:
    import PySpin
except Exception as exc:
    print(f"   import failed: {exc}")
    sys.exit(1)
system = PySpin.System.GetInstance()
cams = system.GetCameras()
n = cams.GetSize()
print(f"   PySpin {getattr(PySpin, '__version__', '?')} imports; {n} camera(s) detected")
for i in range(n):
    c = cams[i]
    print(f"     - {c.TLDevice.DeviceModelName.GetValue()}")
    del c
cams.Clear()
system.ReleaseInstance()
sys.exit(0)
PY
then
  cat <<'DONE'

  Spinnaker is in. Restart the station so it picks up the camera:

      sudo systemctl restart imaging-station

  If it detected 0 cameras, log out and back in (or reboot) -- the
  flirimaging group is not live in your session until you do.

DONE
else
  warn "PySpin did not import. The libraries are in $PREFIX/lib and on the"
  warn "ldconfig path; the message above says what is missing."
  exit 1
fi
