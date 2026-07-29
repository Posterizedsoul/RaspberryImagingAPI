"""Spool on disk, one worker thread, forward to the Jetson.

Capture writes a directory and returns. This module drains it. That is the
whole reason the operator can shoot the next board while the previous one is
still being graded, and it is also the page 4 resilience requirement: if the
Jetson is unreachable the images stay on disk and go up when the link returns.
Nothing is ever dropped because the network was.

Spool layout, one directory per capture:

    spool/<capture_id>/meta.json     status, lighting labels, attempts
    spool/<capture_id>/001_warm.png  the originals, byte-for-byte what goes up
    spool/<capture_id>/thumb_001.jpg for the UI list
    spool/<capture_id>/result.json   written when the grade comes back

A finished capture moves to done/. Anything still in spool/ at startup is
re-enqueued, so a power cut costs nothing.
"""
from __future__ import annotations

import json
import queue
import shutil
import threading
import time
from pathlib import Path

import requests

TERMINAL = {"done", "failed", "no-model"}
MAX_BACKOFF = 30.0
# How many finished captures to reload into the UI list on startup. The images
# themselves all stay on disk regardless -- this is only the visible tail.
HISTORY_ON_DISK = 200
# How long to hold the Jetson request open waiting for the grade. The server
# returns the moment inference lands; this is only the ceiling.
WAIT_SECONDS = 15


class Uplink:
    def __init__(self, root: Path, config) -> None:
        self.spool = root / "spool"
        self.done = root / "done"
        self.spool.mkdir(parents=True, exist_ok=True)
        self.done.mkdir(parents=True, exist_ok=True)
        self._config = config  # callable returning the live config dict
        self._q: queue.Queue[str] = queue.Queue()
        self.captures: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._run = True
        self.jetson_ok: bool | None = None
        self._thread = threading.Thread(target=self._work, daemon=True,
                                        name="uplink")

    # -- public -------------------------------------------------------------

    def start(self) -> None:
        self.recover()
        self._thread.start()

    def stop(self) -> None:
        self._run = False
        self._q.put("")  # unblock the worker so shutdown is prompt

    @property
    def depth(self) -> int:
        return self._q.qsize()

    def dir_for(self, capture_id: str) -> Path:
        """Where a capture lives now -- it migrates to done/ when it finishes."""
        d = self.spool / capture_id
        return d if d.exists() else self.done / capture_id

    def enqueue(self, capture_id: str, meta: dict) -> None:
        with self._lock:
            self.captures[capture_id] = meta
        self._q.put(capture_id)

    def recover(self) -> None:
        """Re-enqueue everything left in the spool, oldest first."""
        for d in sorted(self.spool.iterdir()):
            meta_path = d / "meta.json"
            if not d.is_dir() or not meta_path.exists():
                continue
            meta = json.loads(meta_path.read_text())
            meta["status"] = "queued"
            self.enqueue(meta["capture_id"], meta)
        # History survives a restart, but only the tail of it. capture_id
        # starts with a sortable timestamp, so the newest are simply last.
        # ponytail: at 500 boards a day the done/ tree is the archive; this
        # only feeds the UI list. Point it at a database if you ever need to
        # search history rather than glance at it.
        for d in sorted(self.done.iterdir())[-HISTORY_ON_DISK:]:
            meta_path = d / "meta.json"
            if d.is_dir() and meta_path.exists():
                with self._lock:
                    self.captures[d.name] = json.loads(meta_path.read_text())

    def recent(self, limit: int = 30) -> list[dict]:
        with self._lock:
            items = sorted(self.captures.values(),
                           key=lambda m: m["captured_at"], reverse=True)
        return items[:limit]

    # -- worker -------------------------------------------------------------

    def _work(self) -> None:
        backoff = 1.0
        while self._run:
            capture_id = self._q.get()
            if not capture_id or not self._run:
                continue
            meta = self.captures.get(capture_id)
            if not meta or meta["status"] in TERMINAL:
                continue
            try:
                self._send(meta)
                backoff = 1.0
            except _Permanent as exc:
                self._finish(meta, "failed", str(exc))
            except Exception as exc:
                # The Jetson being down affects every queued capture equally,
                # so stalling the worker here is correct: nothing else would
                # succeed either. Captures keep spooling meanwhile.
                meta["attempts"] = meta.get("attempts", 0) + 1
                meta["status"] = "queued"
                meta["error"] = str(exc)
                self.jetson_ok = False
                self._save(meta)
                self._q.put(capture_id)
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)

    def _send(self, meta: dict) -> None:
        cfg = self._config()
        base = cfg["jetson_url"].rstrip("/")
        headers = {"X-API-Key": cfg["api_key"]}
        d = self.spool / meta["capture_id"]

        meta["status"] = "uploading"
        self._save(meta)

        handles = []
        try:
            data = [("board_id", meta["board_id"]),
                    ("task", cfg.get("task", "classification")),
                    ("captured_at", meta["captured_at"]),
                    ("meta", json.dumps({"app_version": meta.get("app_version", "pi-1"),
                                         "station": cfg.get("board_id_prefix", "rig")}))]
            files = []
            for img in meta["images"]:
                fh = open(d / img["file"], "rb")
                handles.append(fh)
                files.append(("views", (img["file"], fh, "image/png")))
                data.append(("lighting", img["lighting"]))
            r = requests.post(f"{base}/v1/boards", data=data, files=files,
                              headers=headers, timeout=120)
        finally:
            for fh in handles:
                fh.close()

        if 400 <= r.status_code < 500 and r.status_code != 429:
            raise _Permanent(f"{r.status_code} {r.text[:200]}")
        r.raise_for_status()
        self.jetson_ok = True

        if not r.json().get("inference_queued"):
            # No activated model matches this task. Waiting would hang forever,
            # so say so instead. The images are safely stored server-side.
            self._finish(meta, "no-model",
                         "uploaded, but the Jetson has no active model for task "
                         f"{cfg.get('task', 'classification')!r}")
            return

        meta["status"] = "waiting"
        self._save(meta)

        g = requests.get(f"{base}/v1/boards/{meta['board_id']}",
                         params={"wait": WAIT_SECONDS}, headers=headers,
                         timeout=WAIT_SECONDS + 15)
        g.raise_for_status()
        preds = [p for p in g.json().get("predictions", [])
                 if p.get("source") == "server"]
        if not preds:
            raise RuntimeError("no prediction within the wait window")

        p = preds[-1]
        result = {k: p.get(k) for k in
                  ("label", "confidence", "margin", "probs", "latency_ms",
                   "model_id", "model_version", "outputs")}
        (d / "result.json").write_text(json.dumps(result, indent=2))
        meta["result"] = result
        self._finish(meta, "done", None)

    def _finish(self, meta: dict, status: str, error: str | None) -> None:
        meta["status"] = status
        meta["error"] = error
        self._save(meta)
        src = self.spool / meta["capture_id"]
        if src.exists():
            dst = self.done / meta["capture_id"]
            shutil.rmtree(dst, ignore_errors=True)
            shutil.move(str(src), str(dst))

    def _save(self, meta: dict) -> None:
        d = self.dir_for(meta["capture_id"])
        if d.exists():
            (d / "meta.json").write_text(json.dumps(meta, indent=2))
        with self._lock:
            self.captures[meta["capture_id"]] = meta


class _Permanent(Exception):
    """A 4xx. Retrying identical bytes will fail identically, so do not."""
