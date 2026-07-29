"""Runnable check: python test_station.py

No framework, no fixtures. Covers the four things that are actually load
bearing and would fail silently if broken:

  1. a recipe of N images produces N files with the lighting labels in order
  2. the multipart the Jetson receives pairs each view with its own label
     (the server 422s on a mismatch, and a mispairing would train on lies)
  3. capture returns before the upload finishes -- the non-blocking claim
  4. a failed upload keeps the images and a restart re-enqueues them
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
from pathlib import Path

import camera
import lights as lights_mod
import station
import uplink as uplink_mod
from uplink import Uplink

RECIPE = [
    {"lighting": "warm", "exposure_us": 4000, "settle_ms": 0, "gain_db": 0.0},
    {"lighting": "white", "exposure_us": 9000, "settle_ms": 0, "gain_db": 0.0},
    {"lighting": "both", "exposure_us": 6000, "settle_ms": 0, "gain_db": 0.0},
]
CONFIG = {"jetson_url": "http://stub", "api_key": "k", "task": "classification",
          "board_id_prefix": "test", "images": RECIPE}


class Reply:
    def __init__(self, payload, status=200):
        self._payload, self.status_code, self.text = payload, status, "stub"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def setup(tmp: Path) -> Uplink:
    station.config = CONFIG
    station.lights = lights_mod.NullLights()
    station.cam = camera.FakeCamera()
    station.cam.start()
    up = Uplink(tmp, lambda: station.config)
    station.uplink = up
    return up


def test_recipe_writes_labelled_images(tmp: Path) -> None:
    setup(tmp)
    meta = station.run_capture()

    assert len(meta["images"]) == 3, meta
    assert [i["lighting"] for i in meta["images"]] == ["warm", "white", "both"]
    assert meta["status"] == "queued"

    d = tmp / "spool" / meta["capture_id"]
    for n, img in enumerate(meta["images"], 1):
        assert (d / img["file"]).stat().st_size > 0, img
        assert (d / f"thumb_{n:03d}.jpg").exists()
        # the filename carries the label too, so a stray file is obvious on disk
        assert img["lighting"] in img["file"]
    assert json.loads((d / "meta.json").read_text())["board_id"] == meta["board_id"]
    print("ok  recipe writes 3 labelled images + thumbs + meta")


def test_multipart_pairs_each_view_with_its_label(tmp: Path) -> None:
    up = setup(tmp)
    meta = station.run_capture()
    seen = {}

    def post(url, data=None, files=None, **kw):
        seen["lighting"] = [v for k, v in data if k == "lighting"]
        seen["views"] = [f[1][0] for f in files if f[0] == "views"]
        seen["board_id"] = dict(d for d in data if d[0] == "board_id")["board_id"]
        return Reply({"inference_queued": True})

    def get(url, **kw):
        return Reply({"predictions": [
            {"source": "edge", "label": "ignore-me"},
            {"source": "server", "label": "3A", "confidence": 0.89,
             "probs": {"2A": 0.04, "3A": 0.89, "4A": 0.07}},
        ]})

    uplink_mod.requests.post, uplink_mod.requests.get = post, get
    up._send(meta)

    assert seen["lighting"] == ["warm", "white", "both"], seen
    assert len(seen["views"]) == len(seen["lighting"])
    for name, light in zip(seen["views"], seen["lighting"]):
        assert light in name, (name, light)
    assert seen["board_id"].startswith("test-")
    assert meta["status"] == "done"
    assert meta["result"]["label"] == "3A", meta["result"]
    # the edge row must not be mistaken for the server's answer
    assert meta["result"]["confidence"] == 0.89
    assert (up.done / meta["capture_id"] / "result.json").exists()
    print("ok  multipart pairs every view with its own lighting label")


def test_capture_returns_before_upload_finishes(tmp: Path) -> None:
    """The claim is "capture does not wait for the upload", so assert exactly
    that: both captures finish while the worker is still inside the POST.

    Deliberately not a wall-clock bound. Encoding six PNGs takes as long as the
    machine takes, so a time limit measures the CPU rather than the behaviour
    and fails under load while the code is perfectly correct.
    """
    up = setup(tmp)
    entered = threading.Event()
    released = threading.Event()
    finished = []

    def slow_post(*a, **kw):
        entered.set()
        released.wait(30)
        finished.append(True)
        return Reply({"inference_queued": False})

    uplink_mod.requests.post = slow_post
    up.start()

    first = station.run_capture()
    assert entered.wait(20), "the worker never started uploading the first capture"
    second = station.run_capture()      # the operator shoots again immediately

    assert not finished, "capture waited for the upload to finish"
    assert first["capture_id"] != second["capture_id"]
    assert up.depth >= 1, "second capture should be waiting in the queue"

    released.set()
    print("ok  both captures returned while the uplink was still mid-upload")


def test_failed_upload_keeps_images_and_restart_requeues(tmp: Path) -> None:
    up = setup(tmp)
    meta = station.run_capture()
    pngs = sorted(p.name for p in (tmp / "spool" / meta["capture_id"]).glob("*.png"))

    def dead(*a, **kw):
        raise ConnectionError("jetson unplugged")

    uplink_mod.requests.post = dead
    try:
        up._send(meta)
        raise AssertionError("expected the upload to fail")
    except ConnectionError:
        pass

    d = tmp / "spool" / meta["capture_id"]
    assert d.exists(), "a failed upload must not delete the capture"
    assert sorted(p.name for p in d.glob("*.png")) == pngs

    fresh = Uplink(tmp, lambda: station.config)   # as if the Pi rebooted
    fresh.recover()
    assert meta["capture_id"] in fresh.captures
    assert fresh.captures[meta["capture_id"]]["status"] == "queued"
    assert fresh.depth == 1
    print("ok  failed upload keeps the images and a restart re-enqueues them")


def test_delete_removes_capture_and_survives_a_late_save(tmp: Path) -> None:
    up = setup(tmp)
    meta = station.run_capture()
    d = tmp / "spool" / meta["capture_id"]
    assert d.exists()

    assert up.delete(meta["capture_id"]) is True
    assert not d.exists(), "images should be gone from the Pi"
    assert meta["capture_id"] not in up.captures
    assert up.delete(meta["capture_id"]) is False, "second delete is a no-op"

    # An upload already inside _send finishes and calls _save afterwards. That
    # must not put the deleted capture back into the list.
    meta["status"] = "done"
    up._save(meta)
    assert meta["capture_id"] not in up.captures, "deleted capture came back"

    # And the worker must skip it rather than trying to upload missing files.
    def boom(*a, **kw):
        raise AssertionError("worker tried to upload a deleted capture")

    uplink_mod.requests.post = boom
    up.start()
    up._q.put(meta["capture_id"])
    time.sleep(0.5)
    print("ok  delete removes the capture and the worker skips it")


def test_config_validation_rejects_bad_input(tmp: Path) -> None:
    for bad, why in [
        ({"images": []}, "empty recipe"),
        ({"images": [{"lighting": "ultraviolet"}]}, "unknown lighting"),
        ({"images": [{"lighting": "warm", "exposure_us": 0}]}, "zero exposure"),
        ({"images": [{"lighting": "warm", "gain_db": 99}]}, "absurd gain"),
    ]:
        try:
            station.validate_config(bad)
            raise AssertionError(f"validate_config accepted {why}")
        except ValueError:
            pass
    ok = station.validate_config({"images": [{"lighting": "both"}]})
    assert ok["images"][0]["exposure_us"] == 8000, "defaults should fill in"
    print("ok  config validation rejects bad recipes")


def test_light_masks_match_the_schematic(tmp: Path) -> None:
    # WGIR-001: bit0 GP2 warm-L, bit1 GP3 white-L, bit2 GP4 warm-R, bit3 GP5 white-R
    assert lights_mod.MASKS["warm"] == 0b0101
    assert lights_mod.MASKS["white"] == 0b1010
    assert lights_mod.MASKS["both"] == 0b1111
    assert lights_mod.MASKS["off"] == 0
    print("ok  light masks match WGIR-001")


if __name__ == "__main__":
    tests = [test_recipe_writes_labelled_images,
             test_multipart_pairs_each_view_with_its_label,
             test_capture_returns_before_upload_finishes,
             test_failed_upload_keeps_images_and_restart_requeues,
             test_delete_removes_capture_and_survives_a_late_save,
             test_config_validation_rejects_bad_input,
             test_light_masks_match_the_schematic]
    for fn in tests:
        tmp = Path(tempfile.mkdtemp(prefix="station-test-"))
        try:
            fn(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(tests)} passed")
