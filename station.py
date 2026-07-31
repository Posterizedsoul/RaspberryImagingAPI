"""Wood grading station -- Raspberry Pi 4 half.

One web app. The 7" touchscreen runs Chromium in kiosk mode against
localhost, and a laptop plugged into the same switch opens the identical page
at http://raspberrypi.local:8080. Same code, same screen, no second UI to
maintain and nothing extra to build for the "plug in a computer" case -- the
Pi is already an HTTP server on the LAN.

    python station.py            # 0.0.0.0:8080

With no Blackfly and no Pico attached it still runs, on synthetic frames.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

import camera
import lights as lights_mod
from uplink import Uplink

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
DATA = ROOT / "data"
THUMB_WIDTH = 240
PORT = 8080

DEFAULT_CONFIG = {
    "jetson_url": "http://jetson.local:8000",
    "api_key": "",
    "task": "classification",
    "board_id_prefix": "rig1",
    # Page 5: a grader that quietly says 4A at 0.41 is worse than one that says
    # "review this". Below this confidence the UI shows a review state instead
    # of a grade.
    "abstain_below": 0.60,
    "images": [
        {"lighting": "warm", "exposure_us": 8000, "settle_ms": 150, "gain_db": 0.0},
        {"lighting": "white", "exposure_us": 8000, "settle_ms": 150, "gain_db": 0.0},
        {"lighting": "both", "exposure_us": 5000, "settle_ms": 150, "gain_db": 0.0},
    ],
}


# ----------------------------------------------------------------- config --

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def validate_config(cfg: dict) -> dict:
    """The browser is a trust boundary. A bad recipe must not reach the rig."""
    images = cfg.get("images")
    if not isinstance(images, list) or not images:
        raise ValueError("at least one image is required")
    if len(images) > 16:
        raise ValueError("16 images per board is the sane ceiling")
    clean = []
    for i, spec in enumerate(images, 1):
        light = spec.get("lighting")
        if light not in lights_mod.MASKS:
            raise ValueError(f"image {i}: lighting must be one of "
                             f"{sorted(lights_mod.MASKS)}")
        exposure = int(spec.get("exposure_us", 8000))
        settle = int(spec.get("settle_ms", 150))
        gain = float(spec.get("gain_db", 0.0))
        if not 1 <= exposure <= 1_000_000:
            raise ValueError(f"image {i}: exposure_us out of range")
        if not 0 <= settle <= 5000:
            raise ValueError(f"image {i}: settle_ms out of range")
        if not 0 <= gain <= 48:
            raise ValueError(f"image {i}: gain_db out of range")
        clean.append({"lighting": light, "exposure_us": exposure,
                      "settle_ms": settle, "gain_db": gain})
    abstain = float(cfg.get("abstain_below", 0.60))
    if not 0.0 <= abstain <= 1.0:
        raise ValueError("abstain_below must be between 0 and 1")
    out = {
        "jetson_url": str(cfg.get("jetson_url", "")).strip(),
        "api_key": str(cfg.get("api_key", "")).strip(),
        "task": str(cfg.get("task", "classification")).strip() or "classification",
        "board_id_prefix": str(cfg.get("board_id_prefix", "rig1")).strip() or "rig1",
        "abstain_below": abstain,
        "images": clean,
    }
    # Live-view exposure is owned by the Live tab, not this form. Carry it
    # through, or saving Settings would silently reset the preview to black.
    preview = cfg.get("preview")
    if isinstance(preview, dict):
        out["preview"] = preview
    return out


# ------------------------------------------------------------------ state --

config = load_config()
cam = camera.open_camera()
# Restore the operator's live-view exposure. Without an explicit value the
# preview runs at whatever the sensor powered up with, which reads as black.
if isinstance(config.get("preview"), dict):
    cam.set_preview(config["preview"].get("exposure_us",
                                          camera.DEFAULT_PREVIEW_EXPOSURE_US),
                    config["preview"].get("gain_db", 0.0))
lights = lights_mod.open_lights()
uplink = Uplink(DATA, lambda: config)

capture_lock = threading.Lock()
_counter = {"n": 0}
last_error: str | None = None
# Which image of the recipe is being taken, so the UI can say "2 / 3 · white"
# instead of an opaque spinner.
progress = {"n": 0, "total": 0, "lighting": None}


def _next_id() -> str:
    _counter["n"] += 1
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{_counter['n']:04d}"


def run_capture() -> dict:
    """Run the configured recipe, spool it, hand it to the uplink, return.

    Blocks for the length of the recipe (settle time, mostly) and no longer.
    Upload and inference happen on the uplink thread, so the operator can
    press capture again immediately.
    """
    global last_error
    if not capture_lock.acquire(blocking=False):
        raise HTTPException(409, "a capture is already running")
    try:
        cfg = config
        if lights.ok:
            volts = lights.voltage()
            if volts is not None and not (lights_mod.RAIL_MIN <= volts <= lights_mod.RAIL_MAX):
                raise HTTPException(
                    503, f"rail at {volts:.2f} V, outside "
                         f"{lights_mod.RAIL_MIN}-{lights_mod.RAIL_MAX} V")

        capture_id = _next_id()
        board_id = f"{cfg['board_id_prefix']}-{capture_id}"
        d = uplink.spool / capture_id
        d.mkdir(parents=True, exist_ok=True)

        images = []
        try:
            progress.update(n=0, total=len(cfg["images"]), lighting=None)
            for i, spec in enumerate(cfg["images"], 1):
                light = spec["lighting"]
                progress.update(n=i, lighting=light)
                # The gate is open for settle + exposure + margin and the
                # firmware shuts it regardless of what happens up here.
                hold = spec["settle_ms"] + spec["exposure_us"] // 1000 + 500
                lights.on(light, hold)
                time.sleep(spec["settle_ms"] / 1000.0)
                png = cam.grab(spec["exposure_us"], spec.get("gain_db", 0.0))
                lights.off()

                name = f"{i:03d}_{light}.png"
                (d / name).write_bytes(png)
                _write_thumb(png, d / f"thumb_{i:03d}.jpg")
                images.append({"file": name, "lighting": light,
                               "exposure_us": spec["exposure_us"],
                               "settle_ms": spec["settle_ms"],
                               "gain_db": spec.get("gain_db", 0.0)})
        finally:
            lights.off()
            progress.update(n=0, total=0, lighting=None)

        meta = {
            "capture_id": capture_id,
            "board_id": board_id,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "images": images,
            "status": "queued",
            "attempts": 0,
            "error": None,
            "result": None,
        }
        (d / "meta.json").write_text(json.dumps(meta, indent=2))
        uplink.enqueue(capture_id, meta)
        last_error = None
        return meta
    except HTTPException:
        raise
    except Exception as exc:
        last_error = str(exc)
        raise HTTPException(500, str(exc))
    finally:
        capture_lock.release()


def _write_thumb(png: bytes, path: Path) -> None:
    img = Image.open(io.BytesIO(png))
    h = max(1, round(img.height * THUMB_WIDTH / img.width))
    img.resize((THUMB_WIDTH, h), Image.BILINEAR).convert("L").save(
        path, "JPEG", quality=75)


# ------------------------------------------------------------------ routes --

@asynccontextmanager
async def lifespan(app: FastAPI):
    uplink.start()
    lights.on_button = _button_pressed  # the physical start button on GP15
    yield
    uplink.stop()
    lights.stop()
    cam.stop()


def _button_pressed() -> None:
    """GP15 goes through exactly the same path as the on-screen button."""
    threading.Thread(target=_safe_capture, daemon=True).start()


def _safe_capture() -> None:
    global last_error
    try:
        run_capture()
    except HTTPException as exc:
        last_error = exc.detail
    except Exception as exc:
        last_error = str(exc)


app = FastAPI(title="Imaging station", lifespan=lifespan)
# Fonts are served from here. Self-hosted on purpose: the rig LAN has no route
# to a CDN, so a webfont link would silently fall back on the kiosk.
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/preview.mjpg")
def preview() -> StreamingResponse:
    def frames():
        last = None
        while True:
            frame = cam.latest_jpeg()
            if frame is not None and frame is not last:
                last = frame
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n"
                       b"Content-Length: " + str(len(frame)).encode() +
                       b"\r\n\r\n" + frame + b"\r\n")
            time.sleep(1.0 / camera.PREVIEW_FPS)

    return StreamingResponse(
        frames(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.post("/api/capture")
def api_capture() -> dict:
    return run_capture()


@app.get("/api/captures")
def api_captures(limit: int = 30) -> dict:
    return {"captures": uplink.recent(limit), "queue_depth": uplink.depth}


@app.get("/api/captures/{capture_id}/thumb/{n}.jpg")
def api_thumb(capture_id: str, n: int) -> FileResponse:
    if not capture_id.replace("-", "").isalnum():
        raise HTTPException(400, "bad capture id")
    path = uplink.dir_for(capture_id) / f"thumb_{n:03d}.jpg"
    if not path.exists():
        raise HTTPException(404, "no such thumbnail")
    return FileResponse(path, media_type="image/jpeg")


@app.delete("/api/captures/{capture_id}")
def api_delete_capture(capture_id: str) -> dict:
    """Drop a capture from the queue and delete its images from the Pi."""
    if not capture_id.replace("-", "").isalnum():
        raise HTTPException(400, "bad capture id")
    if not uplink.delete(capture_id):
        raise HTTPException(404, "no such capture")
    return {"deleted": capture_id}


@app.post("/api/kiosk/exit")
def api_kiosk_exit() -> dict:
    """Close the full-screen browser and drop to the desktop.

    The station itself keeps running -- this only ends the kiosk, so the UI is
    still there at :8080. Killing the browser is more reliable than
    window.close(), which Chromium refuses for windows it did not open itself.
    """
    if not shutil.which("pkill"):
        raise HTTPException(501, "pkill is not available on this system")
    # --app=<url> is the distinctive part and it survives flag changes; the
    # old "chromium.*--kiosk" pattern missed --window mode and any browser
    # launched with a different flag order, which looked like the button had
    # merely minimised the window. open-ui.sh is the wrapper that exec'd it.
    patterns = [f"--app=http://localhost:{PORT}", "chromium.*--kiosk", "open-ui.sh"]
    results = [subprocess.run(["pkill", "-f", p]).returncode for p in patterns]
    return {"closed": 0 in results}


@app.get("/api/preview")
def api_get_preview() -> dict:
    """Live-view exposure and the range this sensor accepts."""
    return {**cam.preview_settings, **cam.limits()}


@app.put("/api/preview")
def api_set_preview(body: dict) -> dict:
    """Change the live view only. Captures keep using the recipe's own
    exposure, so the page 3 capture contract is untouched by this."""
    try:
        exposure = int(body["exposure_us"])
        gain = float(body.get("gain_db", 0.0))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, "exposure_us and gain_db must be numbers")
    if not 1 <= exposure <= 10_000_000 or not 0 <= gain <= 48:
        raise HTTPException(422, "exposure_us or gain_db out of range")
    out = cam.set_preview(exposure, gain)
    config["preview"] = out
    save_config(config)
    return out


@app.post("/api/preview/auto")
def api_auto_expose() -> dict:
    """Let the sensor find an exposure once, then keep the number.

    This is what SpinView does continuously. Leaving auto on would break the
    capture contract, so it converges once and locks the value in.
    """
    try:
        found = cam.auto_expose()
    except Exception as exc:
        raise HTTPException(503, f"auto exposure failed: {exc}")
    config["preview"] = cam.preview_settings
    save_config(config)
    return found


@app.get("/api/config")
def api_get_config() -> dict:
    return config


@app.put("/api/config")
def api_put_config(new: dict) -> dict:
    global config
    # The Settings form has no preview fields, so carry the current ones over
    # rather than trusting the client to round-trip them.
    if "preview" not in new and "preview" in config:
        new = {**new, "preview": config["preview"]}
    try:
        config = validate_config(new)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    save_config(config)
    return config


@app.get("/api/jetson")
def api_jetson() -> dict:
    """What the Jetson currently has loaded, for the Settings tab.

    Worth showing: the server truncates a board to the active model's
    max_views, so a recipe longer than that silently uploads images the model
    never sees.
    """
    try:
        r = requests.get(f"{config['jetson_url'].rstrip('/')}/v1/models",
                         headers={"X-API-Key": config["api_key"]}, timeout=5)
        r.raise_for_status()
        models = r.json().get("models", [])
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}
    active = next((m for m in models
                   if m.get("active") and m.get("task") == config["task"]), None)
    return {"reachable": True, "active": active,
            "max_views": (active or {}).get("max_views"),
            "recipe_images": len(config["images"])}


@app.get("/api/health")
def api_health() -> dict:
    return {
        "camera": {"ok": cam.ok, "name": cam.name, "error": cam.error},
        "pico": {"ok": lights.ok, "error": lights.error,
                 "volts": lights.voltage() if lights.ok else None,
                 "lid_closed": lights.lid_closed},
        "queue_depth": uplink.depth,
        "jetson_ok": uplink.jetson_ok,
        "capturing": capture_lock.locked(),
        "progress": dict(progress),
        "last_error": last_error,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
