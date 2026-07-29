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

## The Python version

FLIR ship one `spinnaker_python` tarball **per interpreter version**, and they
lag well behind the distro — on a current Raspberry Pi OS there is often no
wheel for the system Python at all. A 3.13 system and a `cp310` wheel is the
normal situation, not a mistake on your part.

So the wheel decides which Python the project runs on, not the other way
round. `install-spinnaker.sh` reads the version out of the wheel and, if it
does not match, obtains that interpreter and rebuilds `.venv` on it:

1. an existing `python3.10` on `PATH`, if there is one;
2. otherwise a pyenv build — **compiles CPython from source, 20–30 minutes on
   a Pi 4**, once. Re-runs reuse it.

Then it reinstalls `requirements.txt` and the wheel. Nothing else needs
touching: the systemd unit, `open-ui.sh` and the tests all reference
`.venv` by path, so they pick up the new interpreter automatically. The
rebuilt venv keeps `--system-site-packages`, so a later `setup.sh` run
recognises it and leaves it alone.

Before waiting on a source build, it is worth a look at FLIR's download page
for a `spinnaker_python` matching a Python you already have — that skips the
whole step.

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

**`_ARRAY_API not found`, or "compiled with numpy 1.x cannot be used in numpy
2.x"** — PySpin's C extension is built against the numpy 1.x ABI. The
libraries are fine; this is purely the Python side:

```bash
.venv/bin/pip install "numpy<2"
```

The script pins this for you and installs the wheel with `--no-deps`, because
the wheel declares an unpinned `numpy` and `--force-reinstall` applies to
dependencies too — without `--no-deps` pip pulls numpy 2 straight back over
the pin.

Everything in this directory is gitignored, deliberately. The SDK is
proprietary Teledyne FLIR software and its licence forbids redistribution, so
it cannot live in this repo — that is why it is a download step rather than a
bundled file.

Without it the station runs on synthetic frames and everything else — capture
recipe, queue, spool, uplink, UI — works normally.
