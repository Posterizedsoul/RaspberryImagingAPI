# RaspberryImagingAPI

The Raspberry Pi 4 half of the wood grading station (WGIR-PLAN rev A). Owns the
Blackfly S camera and the four COB channels, captures a configurable recipe of
images per board, and forwards them to the Jetson for grading.

The Jetson half lives in [`JetsonAPI`](../JetsonAPI).

## Install

On the Pi, clone and run one thing:

```bash
bash setup.sh
```

It installs the system packages, builds the virtualenv, raises the USB buffer,
adds you to `dialout` for the Pico, installs and starts the `imaging-station`
service, and sets the kiosk browser to open the UI full screen on boot. It is
idempotent — re-run it after a `git pull`.

It prints the URLs when it finishes. Set the Jetson URL and ingest key in the
Settings tab and you are done. If it says a reboot is needed, that is the USB
buffer and the `dialout` group; both only take effect after one.

To run it by hand instead:

```bash
.venv/bin/python station.py
```

## One app, two screens

The 7" DSI touchscreen runs Chromium in kiosk mode against `localhost:8080`. A
laptop on the same switch opens the identical page at
`http://raspberrypi.local:8080`.

That is the whole answer to "if I connect a computer to the Pi, show a webpage":
there is nothing to build for it. The Pi is already an HTTP server, and
Raspberry Pi OS ships avahi, so mDNS resolves over a direct ethernet cable with
no router and no DHCP server. One UI, one codebase, no relay.

Light by default, with a dark toggle in the header corner; the choice is
remembered per browser, so the kiosk and your laptop can differ.

### Typography

EB Garamond carries the grade, the tabs and the capture button. Everything
small and functional is the system sans — a delicate serif at 13 px on a 7"
panel is unreadable at arm's length, let alone the two metres page 6 asks for.

The font ships with the repo at `static/fonts/EBGaramond.woff2` (44 KB, latin
subset, variable 400–700) under the SIL Open Font License — `OFL.txt` sits
beside it, which is what redistribution requires. Nothing to install, and it is
served from the Pi rather than a CDN because the rig LAN has no route to one.

If the file ever goes missing the stack falls through to Georgia and then to
the system serif, so the UI degrades quietly rather than breaking.

## Running with nothing attached

With no Spinnaker SDK and no Pico, the station still starts: `FakeCamera`
generates synthetic frames whose brightness tracks the configured exposure, and
`NullLights` accepts light commands and does nothing. The full capture, queue,
upload and result path runs. This is how you develop on a laptop.

Status pills in the header show what is real and what is stubbed.

## Capture flow

1. `POST /api/capture` — the on-screen button, or `BTN` from the Pico's GP15.
2. Per recipe entry: open the light gate, settle, grab, close.
3. PNGs + thumbnails + `meta.json` land in `data/spool/<capture_id>/`.
4. The capture returns **here**, before any network work. The operator can shoot
   the next board immediately.
5. The uplink thread POSTs to `/v1/boards` and holds one `GET
   /v1/boards/<id>?wait=15` until the grade lands.
6. Result written to `result.json`, the directory moves to `data/done/`.

If the Jetson is unreachable the capture stays in `data/spool/` and retries with
backoff. Anything still spooled at startup is re-enqueued, so a power cut or a
pulled cable costs nothing. A grading line that stops because a network cable
moved is worse than one that runs a few boards behind.

## Configuration

`config.json` is written on first run and edited from the Settings tab.

| Field | Meaning |
|---|---|
| `jetson_url` | e.g. `http://jetson.local:8000` |
| `api_key` | an **ingest**-scoped key, sent as `X-API-Key` |
| `task` | must match an activated model's task on the Jetson |
| `board_id_prefix` | identifies this rig in the `board_id` |
| `images[]` | the recipe: one entry per image |

Each recipe entry is `{lighting, exposure_us, settle_ms, gain_db}`. Adding and
removing rows changes how many images per board; `lighting` is one of `warm`,
`white`, `both`, `off` and rides along to the Jetson as that image's label.

`exposure_us`, `settle_ms` and `gain_db` stay per-image and UI-editable on
purpose. A real rig needs tuning that a hardcoded constant cannot see.

`abstain_below` is the confidence under which the UI stops showing a grade and
shows **REVIEW** instead — card and top bar turn amber, and History counts how
many are waiting. Page 5 of the plan is blunt about why: a grader that quietly
outputs 4A at 0.41 is worse than one that says "review this", and a rising
abstain rate is the earliest sign the rig has drifted.

**Watch `max_views`.** The Jetson truncates a board to the active model's
`max_views`. A recipe longer than that uploads images the model never grades —
"Check Jetson" in Settings says so explicitly.

## Operating it

The **CAPTURE** button and the Pico's GP15 do the same thing. While a recipe
runs, the live view shows which image is being taken and under which light
(`2 / 3 · white`) rather than an opaque spinner.

A camera fault or a failed capture raises a banner across the top; it stays
until dismissed, because a grading station that fails quietly is the dangerous
kind (page 8).

**History** lists recent captures with their thumbnails, grade and status, and
a `×` per row that removes a capture from the queue and deletes its images from
the Pi. A capture already mid-upload finishes, but it will not reappear in the
list.

**Settings → Exit full screen** closes the kiosk browser and returns to the
desktop. The station keeps capturing and uploading; the UI is still at
`localhost:8080`.

To get back in — after that, or after Alt+F4 — double-click **Imaging Station**
on the desktop, or find it in the applications menu. The autostart entry only
fires at login, so without that launcher the only way back would be a terminal.
By hand it is:

```bash
chromium --kiosk --app=http://localhost:8080
```

## Endpoints

| Route | Purpose |
|---|---|
| `GET /` | the UI |
| `GET /preview.mjpg` | live MJPEG, pulled straight from the Pi |
| `POST /api/capture` | run the recipe, spool, enqueue, return |
| `GET /api/captures` | recent captures with status and grade |
| `GET /api/captures/{id}/thumb/{n}.jpg` | thumbnails |
| `GET`/`PUT /api/config` | the recipe and connection settings |
| `DELETE /api/captures/{id}` | drop from the queue, remove its images |
| `GET /api/jetson` | active model, for the Settings tab |
| `GET /api/health` | camera, Pico, rail volts, queue depth, capture progress |
| `POST /api/kiosk/exit` | close the kiosk browser, leave the station running |

## Hardware

### Camera

PySpin is not on PyPI and the Spinnaker SDK cannot be redistributed, so it is
not in `requirements.txt` and not in this repo. Download the ARM64 build from
<https://flir.com/products/spinnaker-sdk>, drop both tarballs in `vendor/`, and
re-run `setup.sh` — it installs the debs, the udev rules, the `flirimaging`
group and the matching PySpin wheel unattended. See [vendor/](vendor/README.md).

Until then the station runs on synthetic frames and everything else works.

`setup.sh` raises the USB buffer for you (`usbcore.usbfs_memory_mb=1000` in
`cmdline.txt`); without it a 5 MP sensor returns incomplete frames rather than
errors, which reads as flaky hardware. It needs a reboot.

**One thread owns the camera.** The acquisition thread serves the live preview
and services capture requests between frames. Two threads calling
`GetNextImage()` on one `CameraPtr` is the classic Spinnaker crash, and it
presents as flaky hardware rather than as a bug. Do not add a second one.

Gamma, auto-exposure, auto-gain and auto-white-balance are pinned off at open,
per the page 3 capture contract: anything that rescales pixel values between
training and deployment moves accuracy silently.

### Lighting

Flash `pico/main.py` onto the RP2040 as `main.py`. Channel map is straight off
WGIR-001:

| bit | GPIO | net | fixture |
|---|---|---|---|
| 0 | GP2 | GATE0 | warm left |
| 1 | GP3 | GATE1 | white left |
| 2 | GP4 | GATE2 | warm right |
| 3 | GP5 | GATE3 | white right |

So `warm` = `0b0101`, `white` = `0b1010`, `both` = `0b1111`.

Every `L` command carries a duration and the firmware closes the gates when it
expires, capped at 2 s. Four COB bars is 5.3 A; on-time lives in firmware
precisely so that a crashed Pi cannot leave them lit.

GP15 (SW2) emits `BTN` and triggers a capture through the same path as the
on-screen button. GP16 (SW3) emits `ABORT` and kills the gates immediately.
GP14 is read as a lid switch — **not on WGIR-001 rev A**; wire one there if you
fit it, otherwise it reads open and is reported as such.

Capture is refused if the rail is outside 11.6–12.4 V.

## What setup.sh sets up

`imaging-station.service` waits for `network-online.target` so the Jetson
hostname resolves, and rate-limits the journal so a crash loop cannot fill the
SD card.

The kiosk is a desktop autostart entry rather than a systemd unit, because the
desktop session owns the display — that works under both X11 and Wayland with
no guessing at `DISPLAY`. It waits for the station to answer before opening,
otherwise the operator's first paint is an error page.

The virtualenv is built with `--system-site-packages` deliberately: FLIR's
installer puts PySpin in `/usr/lib/python3/dist-packages`, and an isolated venv
cannot see it — the station would run on synthetic frames forever without
saying anything obvious. Re-running the script rebuilds an isolated venv if it
finds one.

```bash
journalctl -u imaging-station -f
```

## Tests

```bash
python test_station.py
```

Six assertions, no framework. The ones that matter: each view is paired with its
own lighting label in the multipart (the server 422s on a mismatch, and a
mispairing would quietly poison the training set), capture returns before the
upload finishes, and a failed upload keeps the images so a restart re-enqueues
them.

## Not built

Deliberately out of scope for v1: the page 3 capture-contract hash,
dark-frame/flat-field calibration, and page 8 drift monitoring. The hook point
for all three is the frame-conditioning step in `camera.py`, between grab and
encode. Also skipped: lid interlock enforcement (the state is reported but does
not block), operator grade override, and the retraining export.
