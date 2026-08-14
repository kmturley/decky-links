/** Sound playback, in the frontend, because the backend is 512ms too slow.
 *
 * Measured on a Deck: `paplay` costs ~512ms of fixed overhead — process spawn
 * plus a PulseAudio handshake — before a 10ms file is audible, and that figure
 * does not move with file length. Issue #8 asks for feedback within 200ms of
 * hardware detection, which that path cannot do at any file size.
 *
 * The same clip through an HTMLAudioElement here: 4.5ms cold, 0.3ms once
 * preloaded. So the backend keeps deciding *which* sound belongs to an event —
 * it owns the state machine and always has — and only playback moves.
 *
 * Sounds are served from `dist/`, which is the one directory Decky Loader
 * exposes over HTTP (verified: `dist/index.js` 200, `main.py` 403,
 * `assets/logo.png` 404). The build copies assets/sounds there.
 */
import { sharedState } from "../shared";
import { themeAssetUrl } from "./themes";

/** The logical names the backend may ask for. Matching ALLOWED_SOUNDS in
 *  main.py: an unknown name is dropped rather than fetched, so a typo cannot
 *  turn into a request for an arbitrary path. */
const KNOWN = ["scan", "success", "error", "lock", "unlock"] as const;
export type SoundName = (typeof KNOWN)[number];

/** Decky serves plugin files under the plugin's *display* name, spaces and
 *  all — hence the encode. */
const BASE = `http://localhost:1337/plugins/${encodeURIComponent("Decky Links")}/dist/sounds`;

const cache = new Map<string, HTMLAudioElement>();

function element(url: string): HTMLAudioElement {
  let audio = cache.get(url);
  if (!audio) {
    audio = new Audio(url);
    audio.preload = "auto";
    cache.set(url, audio);
  }
  return audio;
}

/** Overrides published by the active theme, if any.
 *
 * Set by the layer when it loads a theme rather than looked up here, so this
 * module stays ignorant of what a theme is: it plays sounds, and something
 * else decides which. Cleared when no theme is on, because a period
 * soundtrack over Steam's own interface is nobody's intention.
 */
let overrides: Record<string, string> = {};
let overrideTheme: string | null = null;

export function setThemeSounds(themeId: string | null, sounds: Record<string, string>): void {
  overrideTheme = themeId;
  overrides = themeId ? sounds : {};
}

async function urlFor(name: SoundName): Promise<string> {
  const file = overrideTheme && sharedState.settings?.custom_visuals
    ? overrides[name]
    : undefined;
  if (file && overrideTheme) {
    const url = await themeAssetUrl(overrideTheme, file);
    if (url) return url;
  }
  return `${BASE}/${name}.flac`;
}

/** Pull every sound into the decoder up front.
 *
 * This is what buys the 0.3ms: a cold element still has to fetch and decode,
 * and doing that at the moment a tag lands is how the feedback ends up late
 * again by a different route.
 */
export function preloadSounds(): void {
  for (const name of KNOWN) {
    void urlFor(name)
      .then((url) => element(url).load())
      .catch((e) => console.warn(`[ Decky Links ] Could not preload ${name}:`, e));
  }
}

/** A theme's own file, for the scene sounds a theme declares itself.
 *
 * Separate from the logical names above because these are the theme's
 * vocabulary rather than the plugin's: nothing in the backend knows what
 * "seek.flac" is, and nothing should have to.
 */
export async function playThemeSound(themeId: string, file: string): Promise<void> {
  const url = await themeAssetUrl(themeId, file);
  if (!url) return;
  try {
    void new Audio(url).play().catch(() => undefined);
  } catch (e) {
    console.warn(`[ Decky Links ] Theme sound ${file} failed:`, e);
  }
}

let loop: HTMLAudioElement | null = null;

/** Start a looping scene sound, replacing whatever was looping before.
 *
 * One voice, deliberately: two loops running at once is what happens when a
 * scene change is missed, and the failure mode of a stuck drive-seek noise is
 * far worse than a missed one. Restarting the same file is a no-op, so a
 * repeated scene does not stutter.
 */
export async function startLoop(themeId: string, file: string): Promise<void> {
  const url = await themeAssetUrl(themeId, file);
  if (!url) return;
  if (loop && loop.src === url && !loop.paused) return;
  stopLoop();
  try {
    loop = new Audio(url);
    loop.loop = true;
    void loop.play().catch(() => undefined);
  } catch (e) {
    console.warn(`[ Decky Links ] Loop ${file} failed:`, e);
    loop = null;
  }
}

export function stopLoop(): void {
  if (!loop) return;
  loop.pause();
  loop = null;
}

/** Play a sound, if we know it.
 *
 * Never awaits and never throws: this is called from event handlers whose real
 * job is launching a game, and a missing audio file must not take that down.
 * A fresh element per play rather than rewinding the cached one, so two events
 * close together overlap instead of cutting each other off — a tag landing
 * while a pairing chime is still ringing should sound like both.
 */
export function playSound(name: string): void {
  if (!KNOWN.includes(name as SoundName)) {
    console.warn(`[ Decky Links ] Unknown sound: ${name}`);
    return;
  }
  void urlFor(name as SoundName).then((url) => {
    try {
      const voice = new Audio(element(url).src);
      voice.volume = 1;
      void voice.play().catch((e) => {
        console.warn(`[ Decky Links ] Sound ${name} did not play:`, e?.name ?? e);
      });
    } catch (e) {
      console.warn(`[ Decky Links ] Sound ${name} failed:`, e);
    }
  });
}
