# MicroPython firmware for the RP2040 on WGIR-001.
#
# Copy to the Pico as main.py. Talks the line protocol in ../lights.py over
# the USB CDC port.
#
#   L<mask> <ms>   set gates, auto-off after ms (mask bit 0..3 = GATE0..3)
#   V              -> "V 12.03"   rail volts through the 100k/33k divider
#   ?              -> "PONG"
#
# Unsolicited, one per line: BTN, ABORT, LID 0|1
#
# The duration on L is not optional and the ceiling below is not advisory.
# Four COB bars is 5.3 A; the gates must close even if the Pi never sends
# another byte, so on-time lives here and not in Python on the other end.

import select
import sys
import time

from machine import ADC, Pin

GATES = [Pin(n, Pin.OUT, value=0) for n in (2, 3, 4, 5)]  # GATE0..GATE3
START = Pin(15, Pin.IN, Pin.PULL_UP)   # SW2, closes to the star point
ABORT = Pin(16, Pin.IN, Pin.PULL_UP)   # SW3
LID = Pin(14, Pin.IN, Pin.PULL_UP)     # not on WGIR-001 rev A -- wire the lid
                                       # switch here if you fit one, else it
                                       # reads open and is reported as such
VSENSE = ADC(26)                       # ADC0, note 11: V(rail) x 33/133

DIVIDER = 4.030
MAX_ON_MS = 2000
DEBOUNCE_MS = 40

off_at = None
last_btn = 0
last_lid = None


def set_mask(mask):
    for i, gate in enumerate(GATES):
        gate.value((mask >> i) & 1)


def volts():
    total = 0
    for _ in range(16):          # the rail is noisy under 5 A of COB
        total += VSENSE.read_u16()
    return (total / 16) * 3.3 / 65535 * DIVIDER


def handle(line):
    global off_at
    line = line.strip()
    if not line:
        return
    if line == "?":
        print("PONG")
    elif line == "V":
        print("V %.2f" % volts())
    elif line[0] == "L":
        try:
            mask_s, _, ms_s = line[1:].partition(" ")
            mask = int(mask_s) & 0x0F
            ms = min(int(ms_s or 0), MAX_ON_MS)
        except ValueError:
            print("ERR bad L")
            return
        set_mask(mask)
        off_at = time.ticks_add(time.ticks_ms(), ms) if mask and ms else None
        if mask and not ms:
            set_mask(0)          # no duration means no light, by design
        print("OK")
    else:
        print("ERR unknown")


poller = select.poll()
poller.register(sys.stdin, select.POLLIN)
buf = ""
print("READY")

while True:
    now = time.ticks_ms()

    if off_at is not None and time.ticks_diff(now, off_at) >= 0:
        set_mask(0)
        off_at = None

    if poller.poll(5):
        ch = sys.stdin.read(1)
        if ch in ("\n", "\r"):
            handle(buf)
            buf = ""
        elif ch is not None:
            buf += ch
            if len(buf) > 64:    # never let a stuck sender exhaust RAM
                buf = ""

    if not START.value() and time.ticks_diff(now, last_btn) > DEBOUNCE_MS:
        last_btn = now
        print("BTN")
        while not START.value():
            time.sleep_ms(5)

    if not ABORT.value():
        set_mask(0)
        off_at = None
        print("ABORT")
        while not ABORT.value():
            time.sleep_ms(5)

    lid = 1 if not LID.value() else 0   # switch closed to ground = lid shut
    if lid != last_lid:
        last_lid = lid
        print("LID %d" % lid)
