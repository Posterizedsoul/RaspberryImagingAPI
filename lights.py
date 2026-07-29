"""Four-channel COB control over the Pico's USB serial link.

Channel map is straight off WGIR-001:

    bit 0  GP2  GATE0  warm  left
    bit 1  GP3  GATE1  white left
    bit 2  GP4  GATE2  warm  right
    bit 3  GP5  GATE3  white right

Every ``on`` carries a duration and the firmware turns the gates off when it
expires. That is deliberate: 5.3 A of COB must not stay lit because the Pi
crashed or a request thread died mid-capture.
"""
from __future__ import annotations

import queue
import threading
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None

MASKS = {"off": 0b0000, "warm": 0b0101, "white": 0b1010, "both": 0b1111}
CHANNEL_NAMES = ("warm-L", "white-L", "warm-R", "white-R")

RAIL_MIN, RAIL_MAX = 11.6, 12.4  # page 8: outside this, refuse to capture


class Lights:
    """Serial client. Also the source of button and lid events."""

    def __init__(self, port: str | None = None, baud: int = 115200) -> None:
        self.port = port
        self.baud = baud
        self.error: str | None = None
        self.lid_closed: bool | None = None
        self.on_button = lambda: None  # station.py replaces this
        self._ser = None
        self._replies: queue.Queue[str] = queue.Queue()
        self._write_lock = threading.Lock()
        self._run = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True,
                                        name="pico")

    # -- public -------------------------------------------------------------

    def start(self) -> None:
        try:
            self._ser = serial.Serial(self.port or _find_port(), self.baud,
                                      timeout=0.2)
            time.sleep(0.3)  # the RP2040 CDC port needs a moment after open
        except Exception as exc:
            self.error = str(exc)
            return
        self._thread.start()

    def stop(self) -> None:
        self._run = False
        if self._ser:
            try:
                self._command("L0 0")
            except Exception:
                pass
            self._ser.close()

    @property
    def ok(self) -> bool:
        return self._ser is not None and self.error is None

    def on(self, lighting: str, ms: int) -> None:
        """Light a named combination for at most `ms`. Unknown name is an error."""
        if lighting not in MASKS:
            raise ValueError(f"unknown lighting {lighting!r}, expected one of "
                             f"{sorted(MASKS)}")
        self._command(f"L{MASKS[lighting]} {int(ms)}")

    def off(self) -> None:
        self._command("L0 0")

    def voltage(self) -> float | None:
        reply = self._command("V")
        if reply and reply.startswith("V "):
            try:
                return float(reply.split()[1])
            except (IndexError, ValueError):
                return None
        return None

    def ping(self) -> bool:
        return self._command("?") == "PONG"

    # -- internals ----------------------------------------------------------

    def _command(self, line: str, timeout: float = 2.0) -> str | None:
        if not self._ser:
            return None
        with self._write_lock:
            while not self._replies.empty():  # drop anything stale
                self._replies.get_nowait()
            self._ser.write((line + "\n").encode())
            self._ser.flush()
            try:
                return self._replies.get(timeout=timeout)
            except queue.Empty:
                self.error = f"no reply to {line!r}"  # page 8: Pico not responding
                return None

    def _read_loop(self) -> None:
        while self._run:
            try:
                raw = self._ser.readline()
            except Exception as exc:
                self.error = str(exc)
                time.sleep(0.5)
                continue
            if not raw:
                continue
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            self.error = None
            if line == "BTN":
                self.on_button()
            elif line == "ABORT":
                self.off()
            elif line.startswith("LID "):
                self.lid_closed = line.split()[1] == "1"
            else:
                self._replies.put(line)


class NullLights(Lights):
    """No Pico attached. The station still runs; captures are just unlit."""

    def __init__(self) -> None:
        super().__init__()
        self.error = "no Pico connected"
        self.last = "off"

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    @property
    def ok(self) -> bool:
        return False

    def on(self, lighting: str, ms: int) -> None:
        if lighting not in MASKS:
            raise ValueError(f"unknown lighting {lighting!r}")
        self.last = lighting

    def off(self) -> None:
        self.last = "off"

    def voltage(self) -> float | None:
        return None

    def ping(self) -> bool:
        return False


def _find_port() -> str:
    """First RP2040 CDC port. Raises if there is none."""
    for p in list_ports.comports():
        if p.vid == 0x2E8A or "Pico" in (p.description or ""):
            return p.device
    raise RuntimeError("no Pico found on any serial port")


def open_lights(port: str | None = None) -> Lights:
    if serial is None:
        return NullLights()
    try:
        if port is None:
            _find_port()
    except Exception:
        return NullLights()
    lights = Lights(port)
    lights.start()
    return lights if lights.ok else NullLights()
