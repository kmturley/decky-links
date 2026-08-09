/** What the plugin is doing, as a theme sees it.
 *
 * This is a *projection* of the plugin's own state machine (Spec §5), not a
 * second copy of it. The backend decides what is true; this decides what that
 * looks and sounds like, which is a different question with a different shape:
 *
 *  - The machine has four states. A theme needs to tell "reading a disk" from
 *    "launching a game" from "that disk is unreadable", all of which are
 *    CARD_PRESENT.
 *  - The machine is per-source — a disk can be reading while the tag reader
 *    sits idle. A theme is one thing on one screen, so this reduces across
 *    sources with a written precedence rather than showing whichever event
 *    happened to land last.
 *
 * One state, two renderers. Sounds and visuals derived separately would drift,
 * and the drift is exactly what a person notices: a click landing under the
 * wrong animation.
 */

/** Ordered by precedence — later beats earlier when two sources disagree.
 *
 * The order is a claim about what matters: a failure outranks progress, and
 * progress outranks idling. LOCKED sits above them all because it is the one
 * state where the device is refusing to do what it looks like it is doing.
 */
export enum Scene {
  /** Nothing has happened for a while. The screensaver state. */
  AMBIENT = "ambient",
  /** A trigger is up and waiting for a medium. */
  READY = "ready",
  /** A medium is being read, validated, parsed. Often over in <200ms. */
  READING = "reading",
  /** A URI is known and the game is starting. Ends when the game paints —
   *  gamescope composites the overlay away, so nothing has to dismiss it. */
  LAUNCHING = "launching",
  /** A game is up. Audio only: the Steam UI layer is not on screen, which is
   *  verified, not assumed — see the issue #8 discussion. */
  IN_GAME = "in_game",
  /** Unreadable medium, blocked URI, unrecognised key. */
  ERROR = "error",
  /** Restricted mode, key absent. Deliberately not an ERROR: taking the key
   *  out is a normal, successful, intended action (§16), and an error sting
   *  every time a parent pockets the key would be wrong. */
  LOCKED = "locked",
}

const PRECEDENCE: Scene[] = [
  Scene.AMBIENT,
  Scene.READY,
  Scene.IN_GAME,
  Scene.READING,
  Scene.LAUNCHING,
  Scene.ERROR,
  Scene.LOCKED,
];

export function outranks(a: Scene, b: Scene): boolean {
  return PRECEDENCE.indexOf(a) > PRECEDENCE.indexOf(b);
}

/** The facts a scene is derived from, all of which the plugin already tracks.
 *
 * Deliberately a plain snapshot rather than a stream: the same inputs must
 * always give the same scene, or a missed event leaves the theme showing
 * something no fact supports.
 */
export interface SceneInputs {
  /** Any source has a medium mid-read. */
  reading: boolean;
  /** A URI has been accepted and a launch is in flight. */
  launching: boolean;
  /** A game is running (Router.MainRunningApp). */
  inGame: boolean;
  /** A medium was unreadable, blocked, or an unrecognised key. */
  failed: boolean;
  /** Restricted mode is on and the key is not present. */
  locked: boolean;
  /** Milliseconds since anything last happened. */
  idleMs: number;
}

/** How long READY has to be uneventful before it becomes AMBIENT.
 *
 * Long enough that it never fires between putting a disk down and picking the
 * next one up. Steam dims the screen on its own timer too — a shorter value
 * here would put two idle animations on screen at once.
 */
export const AMBIENT_AFTER_MS = 90_000;

export function sceneFor(input: SceneInputs): Scene {
  if (input.locked) return Scene.LOCKED;
  if (input.failed) return Scene.ERROR;
  if (input.launching) return Scene.LAUNCHING;
  if (input.reading) return Scene.READING;
  if (input.inGame) return Scene.IN_GAME;
  if (input.idleMs >= AMBIENT_AFTER_MS) return Scene.AMBIENT;
  return Scene.READY;
}

/** A scene change, as the renderers consume it.
 *
 * Both the edge and the level, because the two renderers want different
 * things: a sound is an edge ("clunk" happens *at* insertion), while an
 * animation is a level (the ambient loop plays *while* idle). A model with
 * only one of them cannot express the other without lying about timing.
 */
export interface SceneChange {
  from: Scene | null;
  to: Scene;
  at: number;
}

/** Visuals shorter than this never render.
 *
 * A tag reads in well under 200ms, so a READING animation on a fast tag would
 * appear and be replaced inside a couple of frames — a flicker reads as a
 * fault. Sounds ignore this: they fire on the edge and finish in their own
 * time, which is the whole reason edges and levels are modelled separately.
 */
export const MIN_VISIBLE_MS = 400;

type Listener = (change: SceneChange) => void;

/** The one place the current scene lives.
 *
 * A module-level singleton for the same reason `sharedState` is one: the
 * renderers mount and unmount independently, and a scene owned by a component
 * would reset when that component happened to remount.
 */
class SceneStore {
  private current: Scene | null = null;
  private listeners = new Set<Listener>();
  private lastEventAt = Date.now();

  get scene(): Scene | null {
    return this.current;
  }

  /** Note that something happened, for the AMBIENT timer. */
  touch(): void {
    this.lastEventAt = Date.now();
  }

  get idleMs(): number {
    return Date.now() - this.lastEventAt;
  }

  apply(input: Omit<SceneInputs, "idleMs">): Scene {
    return this.set(sceneFor({ ...input, idleMs: this.idleMs }));
  }

  set(next: Scene): Scene {
    if (next === this.current) return next;
    const change: SceneChange = { from: this.current, to: next, at: Date.now() };
    this.current = next;
    for (const listener of this.listeners) {
      try {
        listener(change);
      } catch (e) {
        // One bad renderer must not stop the other one being told. A theme
        // with a broken asset should lose its animation, not its sound.
        console.error("[ Decky Links ] Scene listener threw:", e);
      }
    }
    return next;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
}

export const scenes = new SceneStore();
