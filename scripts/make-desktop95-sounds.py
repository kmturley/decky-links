"""Synthesise the Desktop 95 theme's sounds into assets/themes/desktop95/sounds/.

    cd /tmp && python3 ~/Sites/decky-links/scripts/make-desktop95-sounds.py
    for f in *.wav; do ffmpeg -y -i "$f" -c:a flac -sample_fmt s32 \\
        -bits_per_raw_sample 24 "assets/themes/desktop95/sounds/${f%.wav}.flac"; done

Every one of these is written here rather than sampled, and that is not only a
size decision. The startup sound, error chord and ding that shipped with the
mid-90s desktop this theme evokes are somebody's copyrighted recordings; a
theme that shipped them would be redistributing them. Bells and a motor are
easy to synthesise, so what this generates is *of the era* without being *from*
it: the same instrument — a struck metal bar, a spinning disc — playing notes
nobody owns.

The palette is a decade on from the DOS theme's. That one is a mechanism and a
PC speaker; this one is a sound card, so its vocabulary is bells with real
partials, and its drive is an optical one — a smooth continuous whirr rather
than a stepper's train of knocks.
"""

import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from synth import render, seed, write_all  # noqa: E402

seed(1995)


def bell(at, hz, amp=1.0, tau=0.45, attack=0.004):
    """A struck metal bar: a fundamental plus inharmonic partials above it.

    The partial ratios are what make this a bell rather than an organ. They are
    slightly inharmonic (3.01, 4.16, 5.43 rather than 3, 4, 5) because a real
    bar's overtones are, and the higher ones decay faster because energy leaves
    a bar quicker at high frequencies. Take either of those away and the result
    sounds like a synthesiser imitating a bell, which is the failure mode here.
    """
    parts = (
        (1.00, 1.00, 1.00),
        (2.00, 0.40, 0.55),
        (3.01, 0.17, 0.34),
        (4.16, 0.08, 0.22),
        (5.43, 0.04, 0.15),
    )
    return [
        {"kind": "sine", "at": at, "amp": amp * a, "tau": tau * t,
         "hz": hz * ratio, "attack": attack}
        for ratio, a, t in parts
    ]


def click(at, amp=1.0):
    """A mouse button: a switch, so it is almost entirely transient.

    Deliberately shorter and brighter than the DOS theme's insert click. That
    one is a disk seating against a spring; this is a 2mm plastic dome, and
    the difference between them is most of what separates the two decades.
    """
    return [
        {"kind": "noise", "at": at, "amp": 0.85 * amp, "tau": 0.0011, "a": 0.75},
        {"kind": "sine", "at": at, "amp": 0.30 * amp, "tau": 0.005, "hz": 1750},
        {"kind": "sine", "at": at, "amp": 0.18 * amp, "tau": 0.009, "hz": 640},
    ]


# ── One-shots ────────────────────────────────────────────────────────────────

# The UI click. Under 40ms, because anything longer stops being feedback and
# starts being a sound effect you notice.
tick = render(click(0.0), dur=0.045, normalise=0.72)

# Success: one bright bell on E6, with a soft octave beneath it for body.
ding = render(bell(0.0, 1318.5, amp=1.0, tau=0.42)
              + bell(0.004, 659.25, amp=0.30, tau=0.36),
              dur=0.75, normalise=0.82)

# Error. A tritone struck at once and then dropped an octave — dissonant on
# purpose, and resolved nowhere, which is what makes it read as a refusal.
# Written here rather than borrowed: the famous one is a copyrighted recording.
chord = render(bell(0.000, 466.16, amp=0.85, tau=0.55)
               + bell(0.000, 659.25, amp=0.70, tau=0.50)
               + bell(0.090, 233.08, amp=0.75, tau=0.75),
               dur=1.15, normalise=0.80)

# Unlock, and the closest this theme comes to a startup sound: an ascending
# major arpeggio over a low swell. Four notes, 140ms apart, attacks long enough
# that it blooms rather than strikes. The melody is a plain C major arpeggio
# from its fifth — about as unownable as a phrase gets.
chime_events = []
for i, hz in enumerate((392.00, 523.25, 659.25, 783.99)):
    chime_events += bell(0.14 * i, hz, amp=0.9 - 0.08 * i, tau=0.85, attack=0.014)
chime_events.append(
    {"kind": "sine", "at": 0.0, "amp": 0.30, "tau": 1.10, "hz": 130.81,
     "attack": 0.22}
)
chime = render(chime_events, dur=1.9, normalise=0.84)

# ── The drive loop ───────────────────────────────────────────────────────────
#
# An optical drive at speed, not a floppy. A CD spins continuously, so there is
# no step rate to hear: what you get is the spindle motor and the tray
# resonating as a steady whirr, with the head sled sliding across every second
# or so. That sled is the only transient in the sound, and it is soft — a
# voice coil dragging, not a stepper snapping.
DRIVE_DUR = 1.28   # a whole number of seconds' worth, so the loop join is silent
drive_events = []
for i in range(int(DRIVE_DUR / 0.02)):
    at = i * 0.02
    # Two slow wobbles at incommensurate rates. One would be a tremolo; two
    # never quite repeat, which is what stops the ear hearing a loop.
    wobble = (1.0 + 0.13 * math.sin(2 * math.pi * 1.3 * at)
              + 0.07 * math.sin(2 * math.pi * 3.7 * at))
    for hz, amp in ((58, 0.26), (116, 0.11), (174, 0.05), (232, 0.025)):
        drive_events.append(
            {"kind": "sine", "at": at, "amp": amp * wobble, "tau": 0.05,
             "hz": hz, "attack": 0.006}
        )
    # Air and bearing noise, dark enough to sit under the hum rather than on
    # top of it — a bright bed here is what makes synthesised machinery hiss.
    drive_events.append(
        {"kind": "noise", "at": at, "amp": 0.05 * wobble, "tau": 0.02, "a": 0.10}
    )
for at in (0.31, 0.88):
    drive_events += [
        {"kind": "sine", "at": at, "amp": 0.16, "tau": 0.030, "hz": 320,
         "attack": 0.005},
        {"kind": "sine", "at": at + 0.035, "amp": 0.11, "tau": 0.025, "hz": 260,
         "attack": 0.005},
    ]
drive = render(drive_events, dur=DRIVE_DUR, normalise=0.62)

if __name__ == "__main__":
    write_all([
        ("tick", tick), ("ding", ding), ("chord", chord),
        ("chime", chime), ("drive", drive),
    ])
