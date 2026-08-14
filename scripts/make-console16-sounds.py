"""Synthesise the 16-Bit Console theme's sounds into assets/themes/console16/sounds/.

    cd /tmp && python3 ~/Sites/decky-links/scripts/make-console16-sounds.py
    for f in *.wav; do ffmpeg -y -i "$f" -c:a flac -sample_fmt s32 \\
        -bits_per_raw_sample 24 "assets/themes/console16/sounds/${f%.wav}.flac"; done

Fourth sound set, and the first one where the *mechanism* is plastic rather
than metal. That is the whole difference between this and the arcade theme's
coin: a cartridge seating in a slot has no ring to it at all. Two shells and a
spring make a dull knock and a sharp click, and any resonance you add on top
turns the console into a cash register.

The chip half is the era's, not the arcade's: four-operator FM was what these
machines had, and its signature is that a "bell" is really a sine whose timbre
brightens and then decays. There is no FM operator in synth.py, so the same
thing is built additively — the partials are simply given separate decays, fast
ones first, which is what FM feedback decay sounds like from the outside.
"""

import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from synth import render, seed, write_all  # noqa: E402

seed(1991)


def knock(at, amp=1.0, hz=210, tau=0.016, bright=0.30):
    """Plastic on plastic: a short dull thud with no ring.

    ``bright`` is the noise filter coefficient, and the reason this does not
    sound like the arcade theme's coin. At 0.7 it is metal; at 0.3 the top has
    been taken off it and what is left is two moulded shells meeting.
    """
    return [
        {"kind": "noise", "at": at, "amp": 0.55 * amp, "tau": 0.0022, "a": bright},
        {"kind": "sine", "at": at, "amp": 0.75 * amp, "tau": tau, "hz": hz},
        {"kind": "sine", "at": at, "amp": 0.30 * amp, "tau": tau * 0.6, "hz": hz * 2.1},
    ]


def fm(at, hz, amp=1.0, tau=0.30):
    """An FM-ish struck tone: bright at the front, sine by the end.

    The upper partials decay several times faster than the fundamental, which
    is what a decaying modulator does to a carrier's spectrum. Harmonic ratios
    rather than the bell's inharmonic ones — this is a chip pretending to be an
    instrument, not a piece of metal.
    """
    parts = ((1.0, 1.00, 1.00), (2.0, 0.45, 0.34), (3.0, 0.26, 0.18),
             (4.0, 0.14, 0.11), (6.0, 0.07, 0.07))
    return [
        {"kind": "sine", "at": at, "amp": amp * a, "tau": tau * t, "hz": hz * ratio,
         "attack": 0.003}
        for ratio, a, t in parts
    ]


def square(at, hz, dur, amp=0.5):
    return [{"kind": "square", "at": at, "amp": amp, "tau": dur, "hz": hz}]


# ── The slot ─────────────────────────────────────────────────────────────────

# Cartridge lock: clunk, then click. The clunk is the shell bottoming out in
# the slot; the click, 55ms later, is the latch. Two events rather than one
# because that gap is the whole character of the sound — a single hit is a
# door closing, and two is something engaging.
latch = render(
    knock(0.000, amp=1.0, hz=196, tau=0.030, bright=0.26)
    + knock(0.012, amp=0.35, hz=150, tau=0.020, bright=0.20)
    + knock(0.055, amp=0.55, hz=520, tau=0.007, bright=0.55),
    dur=0.24, normalise=0.84)

# Eject: the spring lets go and the shell rides back out. Lower, looser, and
# with a second knock as it reaches the top of its travel.
eject = render(
    knock(0.000, amp=0.8, hz=340, tau=0.010, bright=0.5)
    + knock(0.045, amp=1.0, hz=165, tau=0.034, bright=0.24)
    + knock(0.120, amp=0.35, hz=120, tau=0.026, bright=0.18),
    dur=0.30, normalise=0.80)

# ── The chip ─────────────────────────────────────────────────────────────────

# Media accepted: four notes up, cheerful, over almost before you notice — the
# chime a console plays when it has decided the cartridge is real.
chime_events = []
for i, hz in enumerate((523.25, 659.25, 783.99, 1046.50)):
    chime_events += fm(0.062 * i, hz, amp=0.85 - 0.07 * i, tau=0.26)
chime = render(chime_events, dur=0.75, normalise=0.80)

# Power on: a bass thump as the rails come up, then the run of the boot jingle
# over the top of it. The thump is the part that sells it — every one of these
# machines put a low transient through the TV before it drew anything, and it
# is felt more than heard.
power_events = [
    {"kind": "sine", "at": 0.00, "amp": 1.00, "tau": 0.26, "hz": 46, "attack": 0.010},
    {"kind": "sine", "at": 0.00, "amp": 0.40, "tau": 0.18, "hz": 92, "attack": 0.010},
    {"kind": "noise", "at": 0.00, "amp": 0.30, "tau": 0.035, "a": 0.12},
]
for i, hz in enumerate((392.00, 523.25, 659.25, 783.99, 1046.50)):
    power_events += square(0.13 + 0.055 * i, hz, 0.048, amp=0.30)
power_events += fm(0.42, 1046.50, amp=0.55, tau=0.45)
power_events += fm(0.42, 1567.98, amp=0.30, tau=0.38)
power = render(power_events, dur=1.15, normalise=0.86)

# Error: buzz, then ding. Two squares a semitone apart is a beat rather than a
# chord — the roughness is the point — and the little bell at the end is what
# makes it read as a refusal rather than a fault, which matters because an
# unprogrammed tag is a normal thing to hand a console.
buzz_events = []
for i in range(5):
    buzz_events += square(i * 0.052, 155.56, 0.030, amp=0.45)
    buzz_events += square(i * 0.052, 164.81, 0.030, amp=0.45)
buzz_events += fm(0.32, 880.00, amp=0.55, tau=0.20)
buzz = render(buzz_events, dur=0.62, normalise=0.66)

if __name__ == "__main__":
    write_all([
        ("latch", latch), ("eject", eject), ("chime", chime),
        ("power", power), ("buzz", buzz),
    ])
