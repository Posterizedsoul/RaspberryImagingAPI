"""Blackfly S acquisition. One thread owns the camera, always.

Two threads calling GetNextImage() on a single CameraPtr is the classic
Spinnaker failure, and it presents as flaky hardware rather than as a bug. So
the acquisition thread grabs continuously for the live preview and services
capture requests between preview frames. Nothing else in this program touches
PySpin.

If PySpin is missing -- a laptop, CI, a Pi with the SDK not yet installed --
FakeCamera takes over and the rest of the station runs unchanged.
"""
from __future__ import annotations

import glob
import io
import os
import threading
import time

import numpy as np
from PIL import Image

# The GenTL producer path is normally exported by Spinnaker's .deb postinst.
# install-spinnaker.sh unpacks the debs with dpkg -x and never runs those, and
# a systemd service would not inherit a shell export anyway. Point at the
# producer ourselves before importing, or PySpin imports cleanly and then
# System.GetInstance() throws "could not load producer".
if "SPINNAKER_GENTL64_CTI" not in os.environ:
    for _cti in sorted(glob.glob("/opt/spinnaker/**/*.cti", recursive=True)):
        os.environ["SPINNAKER_GENTL64_CTI"] = _cti
        break

try:
    import PySpin
except ImportError:  # no Spinnaker SDK here
    PySpin = None

PREVIEW_WIDTH = 640
PREVIEW_QUALITY = 70
PREVIEW_FPS = 8

# An exposure change lands a frame or two later on this sensor. Throw those
# away or image 2 of a recipe is exposed for image 1.
SETTLE_FRAMES = 2

# How long to wait before re-enumerating a camera that stopped answering.
RECONNECT_SECONDS = 2.0
# Consecutive failures before we assume the handle is dead rather than unlucky.
MAX_FAILS = 3


def _encode_preview(arr: np.ndarray) -> bytes:
    """Downscale first, then hand to PIL.

    Slicing with a stride is a numpy view -- effectively free -- so PIL only
    ever sees a small array. Building an Image from a full 5 MP frame and
    resizing it in PIL was costing more per frame than a Pi 4 can spare, and
    showed up as a preview that lagged seconds behind reality.
    """
    step = max(1, arr.shape[1] // PREVIEW_WIDTH)
    img = Image.fromarray(arr[::step, ::step])
    if img.mode not in ("L", "RGB"):
        img = img.convert("L" if img.mode in ("I;16", "I", "F") else "RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=PREVIEW_QUALITY)
    return buf.getvalue()


def _encode_png(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG", compress_level=1)
    return buf.getvalue()


class Camera:
    """Live preview plus on-demand full-resolution grabs, one thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: bytes | None = None
        self._req: dict | None = None
        self._done = threading.Event()
        self._result: tuple[bytes | None, str | None] = (None, None)
        self._run = True
        self._dev = None
        self.error: str | None = None
        self.name = "no camera"
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="acquisition")

    # -- public -------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._run = False
        self._thread.join(timeout=5)

    def latest_jpeg(self) -> bytes | None:
        with self._lock:
            return self._latest

    @property
    def ok(self) -> bool:
        return self._dev is not None and self.error is None

    def grab(self, exposure_us: int, gain_db: float = 0.0,
             timeout: float = 15.0) -> bytes:
        """Ask the acquisition thread for one full-res PNG. Blocks.

        Callers are serialised by the capture lock in station.py, so at most
        one request is ever in flight.
        """
        with self._lock:
            if self._req is not None:
                raise RuntimeError("a capture is already in flight")
            self._req = {"exposure_us": int(exposure_us), "gain_db": float(gain_db)}
            self._done.clear()
        if not self._done.wait(timeout):
            with self._lock:
                self._req = None
            raise TimeoutError("camera did not deliver a frame")
        png, err = self._result
        if err:
            raise RuntimeError(err)
        assert png is not None
        return png

    # -- thread -------------------------------------------------------------

    def _loop(self) -> None:
        next_preview = 0.0
        fails = 0

        while self._run:
            # Opening lives inside the loop so an unplugged camera is
            # re-enumerated when it comes back. Opening once up front meant a
            # replug left the thread retrying a dead handle forever, which is
            # what "stream is not started" was.
            if self._dev is None:
                try:
                    self._dev = self._open()
                    self.error = None
                    fails = 0
                except Exception as exc:
                    self.error = f"no camera: {exc}"
                    time.sleep(RECONNECT_SECONDS)
                    continue

            with self._lock:
                req = self._req

            if req is not None:
                try:
                    self._result = (self._grab_full(req), None)
                    self.error = None
                    fails = 0
                except Exception as exc:
                    self._result = (None, str(exc))
                    fails += 1
                with self._lock:
                    self._req = None
                self._done.set()
                if fails >= MAX_FAILS:
                    self._drop("capture kept failing")
                continue

            now = time.monotonic()
            if now < next_preview:
                time.sleep(0.005)
                continue
            try:
                arr = self._next_array()
                if arr is not None:
                    with self._lock:
                        self._latest = _encode_preview(arr)
                    self.error = None
                    fails = 0
                next_preview = now + 1.0 / PREVIEW_FPS
            except Exception as exc:
                fails += 1
                self.error = f"preview: {exc}"
                if fails >= MAX_FAILS:
                    self._drop("stream stopped answering")
                else:
                    time.sleep(0.5)

        self._drop(None)

    def _drop(self, why: str | None) -> None:
        """Let go of a camera that stopped answering so the next pass
        re-enumerates it. This is what makes unplug/replug recover."""
        try:
            self._close()
        except Exception:
            pass
        self._dev = None
        if why:
            self.error = f"{why}; reconnecting"
            time.sleep(RECONNECT_SECONDS)

    def _grab_full(self, req: dict) -> bytes:
        self._apply(req["exposure_us"], req["gain_db"])
        for _ in range(SETTLE_FRAMES):
            self._next_array()
        # Page 8: an incomplete frame is discarded and retaken once, never used.
        for attempt in range(2):
            arr = self._next_array()
            if arr is not None:
                return _encode_png(arr)
        raise RuntimeError("two consecutive incomplete frames")

    # -- backend (overridden by FakeCamera) ---------------------------------

    def _open(self):
        if PySpin is None:
            raise RuntimeError("PySpin is not installed")
        system = PySpin.System.GetInstance()
        cams = system.GetCameras()
        if cams.GetSize() == 0:
            cams.Clear()
            system.ReleaseInstance()
            raise RuntimeError("no Blackfly enumerated")
        cam = cams[0]
        cam.Init()
        nodes = cam.GetNodeMap()
        self.name = cam.TLDevice.DeviceModelName.GetValue()

        # The page 3 capture contract: everything that could rescale pixel
        # values between training and deployment is pinned off.
        cam.AcquisitionMode.SetValue(PySpin.AcquisitionMode_Continuous)
        cam.ExposureAuto.SetValue(PySpin.ExposureAuto_Off)
        cam.GainAuto.SetValue(PySpin.GainAuto_Off)
        try:
            PySpin.CBooleanPtr(nodes.GetNode("GammaEnable")).SetValue(False)
        except Exception:
            pass
        try:  # mono models have no white balance node at all
            wb = PySpin.CEnumerationPtr(nodes.GetNode("BalanceWhiteAuto"))
            wb.SetIntValue(wb.GetEntryByName("Off").GetValue())
        except Exception:
            pass

        # Serve the newest frame and bin the backlog. The default queues every
        # frame the sensor produces, so a preview slower than the frame rate
        # falls further behind every second and you end up watching the past.
        # This is the single biggest cause of "the camera feels slow".
        try:
            s = cam.GetTLStreamNodeMap()
            mode = PySpin.CEnumerationPtr(s.GetNode("StreamBufferHandlingMode"))
            mode.SetIntValue(mode.GetEntryByName("NewestOnly").GetValue())
        except Exception:
            pass

        cam.BeginAcquisition()
        self._system, self._cams = system, cams
        return cam

    def _apply(self, exposure_us: int, gain_db: float) -> None:
        self._dev.ExposureTime.SetValue(float(exposure_us))
        self._dev.Gain.SetValue(float(gain_db))

    def _next_array(self) -> np.ndarray | None:
        """One frame. None means the frame was incomplete, caller decides."""
        img = self._dev.GetNextImage(2000)
        try:
            if img.IsIncomplete():
                return None
            return img.GetNDArray().copy()
        finally:
            img.Release()

    def _close(self) -> None:
        # Each step is separately guarded: after an unplug most of these throw,
        # and one failure must not stop the rest from releasing. A System
        # instance left behind means the next enumeration finds nothing.
        for step in (lambda: self._dev.EndAcquisition(),
                     lambda: self._dev.DeInit()):
            try:
                step()
            except Exception:
                pass
        self._dev = None
        for obj, call in ((getattr(self, "_cams", None), "Clear"),
                          (getattr(self, "_system", None), "ReleaseInstance")):
            try:
                if obj is not None:
                    getattr(obj, call)()
            except Exception:
                pass
        self._cams = self._system = None


class FakeCamera(Camera):
    """Synthetic frames so the station runs with no rig attached.

    Brightness tracks exposure, so a capture recipe that varies exposure
    produces visibly different images and the wiring is actually testable.
    """

    def _open(self):
        self.name = "FakeCamera (no PySpin)"
        self._exposure = 8000
        return "fake"

    def _apply(self, exposure_us: int, gain_db: float) -> None:
        self._exposure = exposure_us

    def _next_array(self) -> np.ndarray:
        h, w = 480, 640
        y, x = np.mgrid[0:h, 0:w]
        t = time.time()
        grain = (np.sin(x / 7.0 + np.sin(y / 40.0) * 3.0) * 40 + 128)
        sweep = np.sin((x / 60.0) - t * 2) * 20
        level = np.clip(self._exposure / 8000.0, 0.2, 2.0)
        return np.clip((grain + sweep) * level, 0, 255).astype(np.uint8)

    def _close(self) -> None:
        pass


def open_camera() -> Camera:
    cam = Camera() if PySpin is not None else FakeCamera()
    cam.start()
    return cam
