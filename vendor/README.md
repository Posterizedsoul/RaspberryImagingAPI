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

Everything in this directory is gitignored, deliberately. The SDK is
proprietary Teledyne FLIR software and its licence forbids redistribution, so
it cannot live in this repo — that is why it is a download step rather than a
bundled file.

Without it the station runs on synthetic frames and everything else — capture
recipe, queue, spool, uplink, UI — works normally.
