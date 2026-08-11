import { FC, useEffect, useState } from "react";
import { Scene } from "../lib/presentation";
import type { SceneProps, SceneVisual, Theme } from "../lib/themes";

/** A 1993 PC, rendered rather than filmed.
 *
 * Markup and CSS the whole way down, which is not a compromise here — it is
 * the more faithful medium. A DOS screen *is* text on a grid: 80×25 cells, a
 * hard 16-colour palette, a block cursor blinking on a fixed cadence. Video of
 * it would be larger, softer, locked to whatever resolution it was captured
 * at, and would still have to be re-encoded to say a different game's name.
 * The whole theme is a few KB of components plus 225KB of synthesised audio.
 *
 * The period being aimed at is roughly 1988–1995: MS-DOS 5/6 with its
 * grey-on-black text mode, the blue-and-cyan of the DOS Editor and Norton
 * Commander, and the PC speaker. Not Windows 95 — the plugin's own moment is
 * a disk going into a drive, and by 95 that had stopped being how you started
 * a game.
 */

// ── The screen itself ────────────────────────────────────────────────────────

/** IBM's text-mode palette, the sixteen colours everything below is built from.
 *  Values are the CGA/EGA RGB triples, not approximations: getting the exact
 *  cyan is most of what makes a screenshot of this look right. */
const CGA = {
  black: "#000000",
  blue: "#0000AA",
  green: "#00AA00",
  cyan: "#00AAAA",
  red: "#AA0000",
  brown: "#AA5500",
  grey: "#AAAAAA",
  darkGrey: "#555555",
  brightBlue: "#5555FF",
  brightGreen: "#55FF55",
  brightCyan: "#55FFFF",
  brightRed: "#FF5555",
  yellow: "#FFFF55",
  white: "#FFFFFF",
} as const;

/** The font stack, in order of how much anyone will recognise it.
 *
 * A real bitmap face — Perfect DOS VGA, Fixedsys, IBM VGA 8x16 — is what this
 * wants, and none of them can be shipped: they are either non-redistributable
 * or a font file bigger than every other asset here combined. So it names them
 * first for anyone who has installed one, and falls back to whatever monospace
 * the Deck has, which is DejaVu Sans Mono. The CRT treatment below does more
 * for the impression than the letterforms do.
 */
const DOS_FONT = `"Perfect DOS VGA 437", "IBM VGA 8x16", Fixedsys, "Courier New", monospace`;

const screen: React.CSSProperties = {
  width: "100%",
  height: "100%",
  padding: "3.5vh 4vw",
  boxSizing: "border-box",
  fontFamily: DOS_FONT,
  fontSize: "min(3.1vh, 2.2vw)",
  lineHeight: 1.5,
  color: CGA.grey,
  background: CGA.black,
  letterSpacing: "0.06em",
  textTransform: "uppercase",
  position: "relative",
  overflow: "hidden",
};

/** Scanlines, phosphor bloom and a slow flicker.
 *
 * Overlaid rather than baked into each scene so every screen gets the same
 * treatment and a theme author changing a scene cannot accidentally lose it.
 * pointer-events are already off on the layer above; this adds nothing
 * interactive.
 */
const Crt: FC = () => (
  <>
    <style>{`
      @keyframes dl-dos-flicker {
        0%, 96%, 100% { opacity: 0.92 } 97% { opacity: 0.86 } 98% { opacity: 0.95 }
      }
      @keyframes dl-dos-blink { 0%, 49% { opacity: 1 } 50%, 100% { opacity: 0 } }
      .dl-dos-cursor { animation: dl-dos-blink 1.06s step-end infinite; }
    `}</style>
    {/* Scanlines: a 3px repeating gradient, which at the Deck's 800px height
        lands close to the ~200 visible lines of a 320x200 mode. */}
    <div style={{
      position: "absolute", inset: 0, pointerEvents: "none",
      background: "repeating-linear-gradient(to bottom, rgba(0,0,0,0) 0px, rgba(0,0,0,0) 1px, rgba(0,0,0,0.28) 2px, rgba(0,0,0,0.28) 3px)",
      mixBlendMode: "multiply",
    }} />
    {/* Vignette and bloom — a shadow mask curving away at the corners. */}
    <div style={{
      position: "absolute", inset: 0, pointerEvents: "none",
      background: "radial-gradient(ellipse at 50% 50%, rgba(255,255,255,0.05) 0%, rgba(0,0,0,0.45) 92%)",
      animation: "dl-dos-flicker 7s linear infinite",
    }} />
  </>
);

const Cursor: FC = () => (
  <span className="dl-dos-cursor" style={{ background: CGA.grey, color: CGA.black }}>
    &nbsp;
  </span>
);

/** [██████░░░░] — ten cells, filled with the block characters a DOS program
 *  would have used, because that is what a progress bar was: text. */
const BlockBar: FC<{ percent: number; width?: number }> = ({ percent, width = 20 }) => {
  const filled = Math.max(0, Math.min(width, Math.round((percent / 100) * width)));
  return (
    <span style={{ color: CGA.brightCyan }}>
      [{"\u2588".repeat(filled)}
      <span style={{ color: CGA.darkGrey }}>{"\u2591".repeat(width - filled)}</span>]
      <span style={{ color: CGA.grey }}> {String(Math.round(percent)).padStart(3, " ")}%</span>
    </span>
  );
};

/** A bar that fills but never completes.
 *
 * Deliberately asymptotic: the plugin cannot know how long a load will take,
 * and a bar that reached 100% and sat there would be a lie told in the most
 * irritating possible way. This eases toward the high nineties and waits for
 * the scene to change, which is what actually ends it.
 */
const CreepingBar: FC<{ intervalMs?: number }> = ({ intervalMs = 220 }) => {
  const [pct, setPct] = useState(4);
  useEffect(() => {
    const t = setInterval(() => setPct((p) => p + (97 - p) * 0.09), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return <BlockBar percent={pct} />;
};

const Line: FC<{ color?: string; children: React.ReactNode }> = ({ color, children }) => (
  <div style={{ color: color ?? CGA.grey, whiteSpace: "pre-wrap" }}>{children}</div>
);

// ── The scenes ───────────────────────────────────────────────────────────────

/** The standby screen: a drive listing and a prompt waiting for a disk.
 *
 * Modelled on what you actually saw on a machine sitting idle at a command
 * line — a directory of what is mounted, then a prompt. The instruction is
 * phrased as a DOS message rather than as UI copy, because "Insert disk" in
 * this typeface is the whole joke and "Present a tag or disk" would not be.
 */
const Ready: FC<SceneProps> = () => (
  <div style={screen}>
    <Crt />
    <Line color={CGA.white}>Decky Links Disk Operating System</Line>
    <Line color={CGA.darkGrey}>Version 1.00 (C) 2026</Line>
    <br />
    <Line>Volume in drive A has no label</Line>
    <Line>Directory of A:\</Line>
    <br />
    <Line color={CGA.brightCyan}>{"  NFC      <TAG>        Ready"}</Line>
    <Line color={CGA.brightCyan}>{"  FLOPPY   <DRIVE>      Ready"}</Line>
    <Line color={CGA.darkGrey}>{"  USB      <DRIVE>      Ready"}</Line>
    <br />
    <Line color={CGA.yellow}>Insert disk and press any key to continue</Line>
    <br />
    <Line color={CGA.white}>
      {"A:\\> "}
      <Cursor />
    </Line>
  </div>
);

/** After 90 idle seconds. A screensaver, in the sense the word had then:
 *  something moving so the phosphor did not burn a prompt into the glass. */
const Ambient: FC<SceneProps> = () => {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 2600);
    return () => clearInterval(t);
  }, []);
  // Drifts around the screen on a slow, deliberately un-smooth cadence — the
  // period equivalent moved in whole character cells, not pixels.
  const col = (tick * 7) % 46;
  const row = (tick * 3) % 12;
  return (
    <div style={{ ...screen, color: CGA.green }}>
      <Crt />
      <div style={{ paddingLeft: `${col}ch`, paddingTop: `${row * 1.5}em` }}>
        <Line color={CGA.brightGreen}>
          {"DECKY LINKS "}
          <Cursor />
        </Line>
      </div>
    </div>
  );
};

/** A disk has gone in and is being read. The drive seek loop plays under it. */
const Reading: FC<SceneProps> = ({ trigger }) => (
  <div style={screen}>
    <Crt />
    <Line color={CGA.white}>{"A:\\> DIR"}</Line>
    <br />
    <Line color={CGA.brightCyan}>
      Reading drive {trigger === "nfc" ? "T" : "A"}:
    </Line>
    <br />
    <Line><CreepingBar intervalMs={90} /></Line>
    <br />
    <Line color={CGA.darkGrey}>Please wait...</Line>
  </div>
);

/** The disk was good and the game is coming. */
const Launching: FC<SceneProps> = ({ title }) => (
  <div style={screen}>
    <Crt />
    <Line color={CGA.white}>{"A:\\> "}{(title || "GAME").slice(0, 28)}.EXE</Line>
    <br />
    <Line color={CGA.yellow}>Loading {(title || "program").slice(0, 34)}</Line>
    <br />
    <Line><CreepingBar /></Line>
    <br />
    <Line color={CGA.darkGrey}>
      Do not remove disk <Cursor />
    </Line>
  </div>
);

/** The messages everyone who used these machines can still recite.
 *
 * "Abort, Retry, Fail?" was the DOS critical-error prompt and is the single
 * most recognisable string of the era, which is exactly why it is here and
 * exactly why it must not look interactive — there is nothing to abort. It
 * sits under a plain statement of what went wrong so the joke never gets in
 * front of the information.
 */
const Failure: FC<SceneProps> = ({ trigger }) => (
  <div style={{ ...screen, background: CGA.black }}>
    <Crt />
    <Line color={CGA.brightRed}>
      General failure reading drive {trigger === "nfc" ? "T" : "A"}:
    </Line>
    <Line color={CGA.brightRed}>Abort, Retry, Fail?</Line>
    <br />
    <Line color={CGA.grey}>The disk could not be read.</Line>
    <Line color={CGA.darkGrey}>Remove it and try another.</Line>
    <br />
    <Line color={CGA.white}>
      {"A:\\> "}
      <Cursor />
    </Line>
  </div>
);

/** Restricted mode, in period dress. Blue-on-cyan, the palette of every
 *  full-screen DOS utility that ever wanted to look official. */
const Locked: FC<SceneProps> = () => (
  <div style={{ ...screen, background: CGA.blue, color: CGA.brightCyan }}>
    <Crt />
    <div style={{
      border: `2px solid ${CGA.brightCyan}`, padding: "2.5vh 3vw",
      maxWidth: "34ch", margin: "12vh auto 0",
    }}>
      <Line color={CGA.white}>SYSTEM LOCKED</Line>
      <br />
      <Line color={CGA.brightCyan}>Non-system disk or disk error</Line>
      <Line color={CGA.brightCyan}>Insert key disk and press any key</Line>
    </div>
  </div>
);

// ── The theme ────────────────────────────────────────────────────────────────

const visual = (scene: Scene, render: FC<SceneProps>, extra: Partial<SceneVisual> = {}): SceneVisual => ({
  scene,
  render,
  background: CGA.black,
  immediate: true,
  ...extra,
});

export const DOS_THEME: Theme = {
  id: "dos",
  name: "MS-DOS",
  blurb: "Text mode, block cursors and a drive that seeks",
  scenes: {
    [Scene.READY]: visual(Scene.READY, Ready),
    [Scene.AMBIENT]: visual(Scene.AMBIENT, Ambient, { fadeMs: 900 }),
    // The seek loop, not a one-shot: a drive that stopped making noise while
    // the bar on screen was still filling would read as a fault.
    [Scene.READING]: visual(Scene.READING, Reading, {
      enterSound: "insert.flac",
      loopSound: "seek.flac",
      // Not immediate: a tag reads in well under 200ms, and a DOS screen
      // flashing up for two frames looks like a crash rather than a load.
      immediate: false,
    }),
    [Scene.LAUNCHING]: visual(Scene.LAUNCHING, Launching, { loopSound: "seek.flac" }),
    [Scene.ERROR]: visual(Scene.ERROR, Failure, { enterSound: "error.flac" }),
    [Scene.LOCKED]: visual(Scene.LOCKED, Locked, { enterSound: "error.flac" }),
  },
  sounds: {
    // The plugin's own events, in period. `scan` is the disk seating in the
    // drive; `success` is the PC speaker's single acknowledging beep.
    scan: "insert.flac",
    success: "beep.flac",
    error: "error.flac",
    lock: "eject.flac",
    unlock: "insert.flac",
  },
};
