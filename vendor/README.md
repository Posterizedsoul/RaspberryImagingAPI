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

## If the install fails

Check the architecture first:

```bash
dpkg --print-architecture
```

It must say `arm64`. On a 32-bit image (`armhf`) the arm64 debs cannot
install, and dpkg reports only a generic "sub-process returned an error code
(1)" — which looks like a corrupt download rather than the wrong package.
Either flash the 64-bit Raspberry Pi OS or fetch the armhf build.

To see what actually went wrong:

```bash
sudo apt-get install -f -y
```

`setup.sh` prints the full apt output for these packages rather than hiding
it, and falls back to FLIR's own `install_spinnaker*.sh` — which knows the
package order and answers its own prompts — if a plain apt install fails.

Everything in this directory is gitignored, deliberately. The SDK is
proprietary Teledyne FLIR software and its licence forbids redistribution, so
it cannot live in this repo — that is why it is a download step rather than a
bundled file.

Without it the station runs on synthetic frames and everything else — capture
recipe, queue, spool, uplink, UI — works normally.
