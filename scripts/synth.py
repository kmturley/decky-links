"""A tiny additive synthesiser, shared by the theme sound scripts.

Every sound a theme needs is either a mechanism or an oscillator — a drive
stepping, a case resonating, a PC speaker, a bell — and all four are things
synthesis does well. That is why no theme in this repo ships a recording:
recordings are large, their licences have to be tracked, and a knock made of
three damped sines can be *tuned* when it comes out wrong, which is exactly
what happened to the DOS seek loop twice.

What lives here is the engine: events in, samples out, 44.1k/24-bit stereo to
match the plugin's other sounds. What lives in each make-*-sounds.py is that
theme's *timbre* — its own idea of what a click is. Those are not shareable,
and pretending otherwise is how one theme's tweak quietly changes another's.

Loops must be built to a whole number of their own period, or the join clicks.
"""

import math
import random
import struct

SR = 44100


def seed(value):
    """Fix the noise, so regenerating a theme does not change how it sounds.

    Called by each script rather than set here: the seed belongs to a theme's
    sound set, and a shared one would mean adding a sound to one theme altered
    every noise burst in another.
    """
    random.seed(value)


def render(events, dur, normalise=0.89):
    """Sum a list of events into one mono buffer, peak-normalised.

    Three kinds of event, each a dict with ``at`` (seconds) and ``amp``:

    ``sine``    ``hz``, ``tau`` decay constant, optional ``attack`` ramp
    ``noise``   ``tau``, ``a`` one-pole coefficient — 1.0 is white, 0.2 is dark
    ``square``  ``hz``, ``tau`` used as a flat duration rather than a decay
    """
    n = int(SR * dur)
    buf = [0.0] * n

    for ev in events:
        start = int(ev["at"] * SR)
        span = int(ev["tau"] * 6 * SR)
        if ev["kind"] == "noise":
            a, prev = ev["a"], 0.0
            for i in range(span):
                j = start + i
                if j >= n:
                    break
                prev = a * random.uniform(-1, 1) + (1 - a) * prev
                buf[j] += ev["amp"] * prev * math.exp(-i / (ev["tau"] * SR))
        elif ev["kind"] == "square":
            # A PC speaker could only do this: full-swing square, no envelope
            # to speak of beyond the on/off, which is why it sounds the way it
            # does and why softening it would be wrong.
            for i in range(int(ev["tau"] * SR)):
                j = start + i
                if j >= n:
                    break
                phase = (ev["hz"] * i / SR) % 1.0
                buf[j] += ev["amp"] * (1.0 if phase < 0.5 else -1.0)
        else:
            attack = max(1, int(ev.get("attack", 0.0) * SR))
            for i in range(span):
                j = start + i
                if j >= n:
                    break
                t = i / SR
                # An attack ramp, because a sine starting at full amplitude is
                # a step change — which is a click, and clicks are exactly what
                # some of these sounds had too many of.
                rise = min(1.0, i / attack)
                buf[j] += (ev["amp"] * rise * math.sin(2 * math.pi * ev["hz"] * t)
                           * math.exp(-t / ev["tau"]))

    for i in range(int(0.001 * SR)):
        buf[i] *= i / (0.001 * SR)
    tail = int(0.004 * SR)
    for i in range(tail):
        buf[n - 1 - i] *= i / tail

    peak = max((abs(v) for v in buf), default=1.0) or 1.0
    return [v / peak * normalise for v in buf]


def write_wav(path, samples):
    """24-bit stereo WAV, the same mono buffer in both channels."""
    data = bytearray()
    for v in samples:
        q = int(max(-1.0, min(1.0, v)) * 8388607)
        b = struct.pack("<i", q)[:3]
        data += b + b
    hdr = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
           + struct.pack("<IHHIIHH", 16, 1, 2, SR, SR * 2 * 3, 6, 24)
           + b"data" + struct.pack("<I", len(data)))
    with open(path, "wb") as f:
        f.write(hdr + data)


def write_all(sounds):
    """Write every (name, samples) pair into the current directory."""
    for name, samples in sounds:
        write_wav(f"{name}.wav", samples)
        print(f"{name}.wav  {len(samples) / SR:.2f}s")
