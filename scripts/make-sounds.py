"""Synthesise assets/sounds/lock.flac and unlock.flac — a deadbolt, and a latch
springing open.

Kept because the sounds are generated rather than sampled: without the recipe,
changing one means starting from nothing. Pure stdlib — a damped-sine body plus
short filtered noise transients, which is what a mechanical click actually is —
writing 44.1k/24-bit stereo WAVs to match the sounds already in assets/sounds.

    python3 scripts/make-sounds.py
    ffmpeg -y -i lock.wav -c:a flac -sample_fmt s32 -bits_per_raw_sample 24 \
        assets/sounds/lock.flac

FLAC because paplay reads it through libsndfile and the rest of the set is
FLAC; the encode is the one step this script leaves to ffmpeg.
"""
import math, random, struct, os

SR = 44100
random.seed(7)   # reproducible: regenerating must not silently change the sound


def render(events, dur):
    n = int(SR * dur)
    buf = [0.0] * n

    for ev in events:
        start = int(ev["at"] * SR)
        if ev["kind"] == "noise":
            # One-pole lowpass over white noise: `a` is how bright the knock is.
            a, prev = ev["a"], 0.0
            for i in range(int(ev["tau"] * 6 * SR)):
                j = start + i
                if j >= n:
                    break
                prev = a * (random.uniform(-1, 1)) + (1 - a) * prev
                buf[j] += ev["amp"] * prev * math.exp(-i / (ev["tau"] * SR))
        else:
            for i in range(int(ev["tau"] * 6 * SR)):
                j = start + i
                if j >= n:
                    break
                t = i / SR
                buf[j] += (ev["amp"] * math.sin(2 * math.pi * ev["hz"] * t)
                           * math.exp(-t / ev["tau"]))

    # 1ms in, 8ms out: a hard edge at either end is a click of its own.
    for i in range(int(0.001 * SR)):
        buf[i] *= i / (0.001 * SR)
    tail = int(0.008 * SR)
    for i in range(tail):
        buf[n - 1 - i] *= i / tail

    peak = max(abs(v) for v in buf) or 1.0
    return [v / peak * 0.89 for v in buf]    # peak -1 dBFS, in step with the sounds already here


def write_wav(path, samples):
    data = bytearray()
    for v in samples:
        s = max(-1.0, min(1.0, v))
        q = int(s * 8388607)
        b = struct.pack("<i", q)[:3]
        data += b + b                           # same signal both channels
    hdr = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
           + struct.pack("<IHHIIHH", 16, 1, 2, SR, SR * 2 * 3, 6, 24)
           + b"data" + struct.pack("<I", len(data)))
    with open(path, "wb") as f:
        f.write(hdr + data)


# A deadbolt: the bolt travels, then seats. Low, blunt, two beats.
lock = render([
    {"kind": "noise", "at": 0.000, "amp": 0.55, "tau": 0.006, "a": 0.35},
    {"kind": "sine",  "at": 0.000, "amp": 0.50, "tau": 0.045, "hz": 165},
    {"kind": "noise", "at": 0.055, "amp": 1.00, "tau": 0.004, "a": 0.50},
    {"kind": "sine",  "at": 0.055, "amp": 0.60, "tau": 0.060, "hz": 120},
    {"kind": "sine",  "at": 0.055, "amp": 0.25, "tau": 0.025, "hz": 240},
], dur=0.22)

# A latch springing open: brighter, and it rises where the bolt fell.
unlock = render([
    {"kind": "noise", "at": 0.000, "amp": 0.70, "tau": 0.003, "a": 0.60},
    {"kind": "sine",  "at": 0.000, "amp": 0.35, "tau": 0.030, "hz": 320},
    {"kind": "noise", "at": 0.045, "amp": 0.30, "tau": 0.0025, "a": 0.75},
    {"kind": "sine",  "at": 0.045, "amp": 0.50, "tau": 0.090, "hz": 523},
    {"kind": "sine",  "at": 0.045, "amp": 0.25, "tau": 0.060, "hz": 784},
], dur=0.20)

out = os.getcwd()
write_wav(os.path.join(out, "lock.wav"), lock)
write_wav(os.path.join(out, "unlock.wav"), unlock)
print("written")
