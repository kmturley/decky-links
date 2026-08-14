"""Synthesise the DOS theme's sound set into assets/themes/dos/sounds/.

Every one of these is a mechanism or a square wave, which is exactly what
synthesis is good at — and why the theme ships no recordings. A 3.5" drive
seeking is a stepper motor: a train of identical clicks at a fixed step rate,
each one a damped resonance of the head assembly plus the case around it. A PC
speaker is a square wave through a paper cone and nothing else.

Pure stdlib, 44.1k/24-bit stereo, matching the plugin's other sounds.

    cd /tmp && python3 ~/Sites/decky-links/scripts/make-dos-sounds.py
    for f in *.wav; do ffmpeg -y -i "$f" -c:a flac -sample_fmt s32 \\
        -bits_per_raw_sample 24 "assets/themes/dos/sounds/${f%.wav}.flac"; done

The loops (seek) are built to a whole number of steps so the join is silent.
"""

import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from synth import SR, render, seed, write_all  # noqa: E402

seed(1985)   # reproducible: regenerating must not change the sound


def step(at, amp=1.0):
    """One movement of the head, as a *click*: sharp, bright, immediate.

    Right for a disk seating in the drive, where the sound really is plastic
    hitting plastic. Wrong for a seek — see thud() and the note above the seek
    loop for why those are different sounds.
    """
    return [
        {"kind": "noise",  "at": at, "amp": 0.9 * amp, "tau": 0.0016, "a": 0.55},
        {"kind": "sine",   "at": at, "amp": 0.5 * amp, "tau": 0.010,  "hz": 190},
        {"kind": "sine",   "at": at, "amp": 0.3 * amp, "tau": 0.020,  "hz": 96},
    ]


def thud(at, amp=1.0):
    """One movement of the head as you hear it through a case: low and soft.

    The first version of the seek loop was built from step() and came out
    gritty and sharp — a bright noise transient at 190Hz repeated forty times a
    second, which is a Geiger counter, not a floppy drive. What you actually
    hear from across a room is the *case* responding to the motor: a soft
    low-frequency thud around 70-120Hz with the click almost entirely absorbed
    by the plastic on the way out.

    Second pass, after the first was still too present: the noise is gone
    entirely rather than merely quiet, because any broadband content at all
    reads as a click and the case absorbs practically all of it. The
    resonances drop another fourth (52/86/120Hz), the decays roughly double so
    each movement blooms instead of snapping, and the attacks lengthen to
    6-8ms, which is past the ear's threshold for hearing an onset as a
    transient at all. What is left is a soft knock with no edge on either end.
    """
    return [
        {"kind": "sine", "at": at, "amp": 0.60 * amp, "tau": 0.090, "hz": 52,
         "attack": 0.008},
        {"kind": "sine", "at": at, "amp": 0.28 * amp, "tau": 0.060, "hz": 86,
         "attack": 0.007},
        {"kind": "sine", "at": at, "amp": 0.09 * amp, "tau": 0.030, "hz": 120,
         "attack": 0.006},
    ]


# ── The seek loop ────────────────────────────────────────────────────────────
#
# A 3.5" drive seeks in bursts: a run of steps, then a pause while it reads,
# then another run. The rate matters as much as the timbre — at 24ms a burst
# reads as a grind, at 45 as a rattle, and at 65 the movements are far enough
# apart that each one is heard as a separate soft knock, which is the thing
# being imitated. Built to a whole number of beats so the loop join is silent.
STEP_MS = 0.065
seek_events = []
t = 0.0
for burst in range(3):
    for i in range(5):
        seek_events += thud(t, amp=0.85 + 0.15 * random.random())
        t += STEP_MS
    t += 0.22          # the read pause between bursts
seek_dur = t
# The spindle underneath it all, and the main body of the sound now rather
# than a bed under it: a 300rpm hub is a continuous 5Hz rotation whose
# audible part is the motor and the case resonating, not the rotation itself.
# The slow amplitude wobble is deliberate — a real spindle is never perfectly
# balanced, and a mathematically steady hum is the one thing that gives
# synthesis away.
for i in range(int(seek_dur / 0.02)):
    at = i * 0.02
    wobble = 1.0 + 0.16 * math.sin(2 * math.pi * 1.7 * at)
    seek_events.append(
        {"kind": "sine", "at": at, "amp": 0.22 * wobble, "tau": 0.05, "hz": 43,
         "attack": 0.006}
    )
    seek_events.append(
        {"kind": "sine", "at": at, "amp": 0.07 * wobble, "tau": 0.05, "hz": 86,
         "attack": 0.006}
    )

# ── One-shots ────────────────────────────────────────────────────────────────

insert = render(
    step(0.0) + step(0.012, 0.7) + [
        # The shutter sliding back, then the disk seating against the spring.
        {"kind": "noise", "at": 0.000, "amp": 0.45, "tau": 0.030, "a": 0.28},
        {"kind": "noise", "at": 0.070, "amp": 1.00, "tau": 0.004, "a": 0.60},
        {"kind": "sine",  "at": 0.070, "amp": 0.55, "tau": 0.040, "hz": 140},
    ], dur=0.28)

eject = render([
    # The button, then the spring throwing the disk clear.
    {"kind": "noise", "at": 0.000, "amp": 0.70, "tau": 0.004, "a": 0.55},
    {"kind": "sine",  "at": 0.000, "amp": 0.40, "tau": 0.030, "hz": 220},
    {"kind": "noise", "at": 0.045, "amp": 0.85, "tau": 0.055, "a": 0.30},
    {"kind": "sine",  "at": 0.045, "amp": 0.35, "tau": 0.070, "hz": 130},
] + step(0.150, 0.6), dur=0.32)

# The PC speaker. One frequency, no envelope, no apology.
beep = render([{"kind": "square", "at": 0.0, "amp": 0.5, "tau": 0.12, "hz": 880}],
              dur=0.14, normalise=0.6)

# Two low beeps: the sound of "Abort, Retry, Fail?"
error = render([
    {"kind": "square", "at": 0.00, "amp": 0.5, "tau": 0.16, "hz": 233},
    {"kind": "square", "at": 0.22, "amp": 0.5, "tau": 0.22, "hz": 175},
], dur=0.46, normalise=0.6)

seek = render(seek_events, dur=seek_dur)

if __name__ == "__main__":
    write_all([
        ("seek", seek), ("insert", insert), ("eject", eject),
        ("beep", beep), ("error", error),
    ])
