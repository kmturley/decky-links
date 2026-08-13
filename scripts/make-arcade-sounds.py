"""Synthesise the Arcade theme's sounds into assets/themes/arcade/sounds/.

    cd /tmp && python3 ~/Sites/decky-links/scripts/make-arcade-sounds.py
    for f in *.wav; do ffmpeg -y -i "$f" -c:a flac -sample_fmt s32 \\
        -bits_per_raw_sample 24 "assets/themes/arcade/sounds/${f%.wav}.flac"; done

Third sound set, third instrument. The DOS theme is a stepper motor and a PC
speaker; Desktop 95 is a sound card playing bells; this is a coin mechanism and
a square-wave chip, which is a happy coincidence for synthesis because both are
things you can write down exactly.

A coin going into a chute is the interesting one. It is not a single event: a
disc hits the plate, rings, bounces two or three times at an accelerating rate
as it loses height, then rattles down the metal chute and lands in the box. The
ring is inharmonic — a disc, not a string — and the acceleration of the bounces
is most of what makes it read as a real coin rather than a sample of one played
back.
"""

import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from synth import render, seed, write_all  # noqa: E402

seed(1983)


def ring(at, hz, amp=1.0, tau=0.10):
    """Struck metal disc: the fundamental plus partials that are not multiples.

    A coin's overtones are spaced by the modes of a flat disc, which land
    nowhere near the harmonic series. Using 1/2/3 here would give a chime; the
    ratios below are what makes it sound like currency.
    """
    parts = ((1.00, 1.00, 1.00), (1.59, 0.62, 0.72), (2.30, 0.40, 0.52),
             (3.41, 0.22, 0.33), (4.62, 0.11, 0.22))
    return [
        {"kind": "sine", "at": at, "amp": amp * a, "tau": tau * t,
         "hz": hz * ratio, "attack": 0.0012}
        for ratio, a, t in parts
    ]


def knock(at, amp=1.0, hz=150, tau=0.02):
    """Metal against metal, with no ring: the chute, the door, the box."""
    return [
        {"kind": "noise", "at": at, "amp": 0.7 * amp, "tau": 0.0035, "a": 0.5},
        {"kind": "sine", "at": at, "amp": 0.5 * amp, "tau": tau, "hz": hz},
    ]


def chip(at, hz, dur, amp=0.5):
    """One note from a square-wave chip. No envelope, because it had none."""
    return [{"kind": "square", "at": at, "amp": amp, "tau": dur, "hz": hz}]


# ── The coin ─────────────────────────────────────────────────────────────────
#
# Strike, then three bounces at 62% of the previous gap and 55% of the previous
# amplitude — a disc losing height. Then the chute: three knocks getting
# quieter and lower as it travels away from the player, and the box at the end.
coin_events = []
at, gap, amp = 0.0, 0.085, 1.0
for _ in range(4):
    coin_events += ring(at, 2180, amp=amp, tau=0.085 * amp)
    coin_events += knock(at, amp=0.35 * amp, hz=420, tau=0.010)
    at += gap
    gap *= 0.62
    amp *= 0.55
for i, (t, a, hz) in enumerate(((0.30, 0.5, 380), (0.36, 0.36, 320), (0.42, 0.26, 260))):
    coin_events += knock(t, amp=a, hz=hz, tau=0.014)
    coin_events += ring(t, 1650 - 180 * i, amp=0.18 * a, tau=0.05)
coin_events += knock(0.50, amp=0.55, hz=110, tau=0.045)
coin = render(coin_events, dur=0.66, normalise=0.85)

# The reject: the coin never gets past the door. One hit, one rattle back out,
# nothing that rings for long — a mechanism refusing rather than accepting.
clack = render(
    knock(0.0, amp=1.0, hz=240, tau=0.028)
    + ring(0.0, 1400, amp=0.35, tau=0.035)
    + knock(0.075, amp=0.6, hz=180, tau=0.030)
    + knock(0.130, amp=0.3, hz=140, tau=0.020),
    dur=0.30, normalise=0.82)

# ── The chip ─────────────────────────────────────────────────────────────────

# Credit accepted: two notes up, fast. The sound a cabinet makes when it
# decides you may play.
fanfare = render(chip(0.00, 1046.50, 0.055) + chip(0.06, 1567.98, 0.090),
                 dur=0.18, normalise=0.55)

# Boot. An ascending arpeggio, then the octave held — a board coming up and
# telling you it passed its own self-test. Written as one voice because that is
# all most of these had.
boot_events = []
for i, hz in enumerate((523.25, 659.25, 783.99, 1046.50)):
    boot_events += chip(0.085 * i, hz, 0.075)
boot_events += chip(0.34, 1567.98, 0.30, amp=0.45)
# A second voice a fifth below for the held note only, which is as close to
# harmony as a two-channel board usually got.
boot_events += chip(0.34, 1046.50, 0.30, amp=0.28)
boot = render(boot_events, dur=0.70, normalise=0.60)

# Error. A square wave low enough to be felt rather than heard as a pitch,
# gated hard so it stutters — the buzz every board made when it did not like
# what you gave it.
buzz_events = []
for i in range(6):
    buzz_events += chip(i * 0.055, 98.00, 0.030, amp=0.5)
buzz_events += chip(0.36, 82.41, 0.16, amp=0.5)
buzz = render(buzz_events, dur=0.56, normalise=0.62)

# ── The room ─────────────────────────────────────────────────────────────────
#
# An arcade at the far end of a hall: mains hum from a row of cabinets, the
# fluorescent tubes above them, and other machines' attract loops arriving as
# muffled, unresolvable pitches. Quiet on purpose — this plays under an idle
# Deck for as long as it stays idle, and anything with a discernible tune would
# be unbearable by the fourth loop.
ROOM_DUR = 4.0
room_events = []
for i in range(int(ROOM_DUR / 0.02)):
    t = i * 0.02
    drift = 1.0 + 0.10 * math.sin(2 * math.pi * 0.37 * t) + 0.06 * math.sin(2 * math.pi * 1.9 * t)
    for hz, amp in ((60, 0.30), (120, 0.13), (180, 0.06), (240, 0.03)):
        room_events.append({"kind": "sine", "at": t, "amp": amp * drift,
                            "tau": 0.05, "hz": hz, "attack": 0.008})
    room_events.append({"kind": "noise", "at": t, "amp": 0.055 * drift,
                        "tau": 0.02, "a": 0.07})
# Distant machines. Sines rather than squares: a square wave two rooms away has
# had its edges taken off it by everything in between, and a sharp one here
# would sound like it was in this room instead.
for t, hz in ((0.55, 880), (0.72, 1174), (1.90, 659), (2.15, 987),
              (3.10, 784), (3.28, 1046)):
    room_events.append({"kind": "sine", "at": t, "amp": 0.035, "tau": 0.09,
                        "hz": hz, "attack": 0.02})
room = render(room_events, dur=ROOM_DUR, normalise=0.34)

if __name__ == "__main__":
    write_all([
        ("coin", coin), ("clack", clack), ("fanfare", fanfare),
        ("boot", boot), ("buzz", buzz), ("room", room),
    ])
