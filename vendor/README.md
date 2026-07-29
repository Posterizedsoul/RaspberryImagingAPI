# vendor/

Put the two Spinnaker SDK tarballs here and `setup.sh` installs them
unattended — debs, udev rules, the `flirimaging` group and the PySpin wheel.

```
spinnaker-<ver>-arm64-pkg.tar.gz
spinnaker_python-<ver>-cp<XY>-cp<XY>-linux_aarch64.tar.gz
```

Get them from <https://flir.com/products/spinnaker-sdk> (free account). Take
the **ARM64 / aarch64** build for Ubuntu, not amd64, and match the
`spinnaker_python` build to the Pi's Python — `cp311` for Bookworm's 3.11.
`setup.sh` checks and tells you if it does not match.

## How it gets installed

`setup.sh` calls `install-spinnaker.sh`, which you can also run on its own:

```bash
./install-spinnaker.sh
```

**It does not use apt.** FLIR build these debs against Ubuntu 20.04/22.04 and
Raspberry Pi OS is Debian — Bookworm ships `libavcodec59` where the packages
demand `libavcodec58`, so apt hits an unmet dependency it can never satisfy
and leaves half-configured packages behind. No amount of `--fix-broken` helps,
because the package it wants does not exist in your repos.

So instead the script uses `dpkg -x`, which unpacks a deb's *files* with no
dependency resolution, no maintainer scripts and nothing written to apt's
state. Spinnaker is shared libraries plus a udev rule; the dependency metadata
is what fights you, not the software. The libraries go to `/opt/spinnaker/lib`
and onto the `ldconfig` path — not `LD_LIBRARY_PATH`, because the station runs
as a systemd service and would not inherit a shell variable.

It also cleans up a previous failed apt attempt, installs the udev rule and
the `flirimaging` group, reports any shared library that still does not
resolve, installs the matching wheel, and finishes by importing PySpin and
listing the cameras it can see.

## If it still fails

Architecture first — it must say `arm64`:

```bash
dpkg --print-architecture
```

On a 32-bit image the arm64 debs cannot work at all, and dpkg reports only a
generic "sub-process returned an error code (1)", which reads like a corrupt
download rather than the wrong package.

**0 cameras detected** after a successful install is almost always the
`flirimaging` group not being live in your session yet. Log out and back in,
or reboot.

Everything in this directory is gitignored, deliberately. The SDK is
proprietary Teledyne FLIR software and its licence forbids redistribution, so
it cannot live in this repo — that is why it is a download step rather than a
bundled file.

Without it the station runs on synthetic frames and everything else — capture
recipe, queue, spool, uplink, UI — works normally.
