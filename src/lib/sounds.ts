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

/** The logical names the backend may ask for. Matching ALLOWED_SOUNDS in
 *  main.py: an unknown name is dropped rather than fetched, so a typo cannot
 *  turn into a request for an arbitrary path. */
const KNOWN = ["scan", "success", "error", "lock", "unlock"] as const;
export type SoundName = (typeof KNOWN)[number];

/** Decky serves plugin files under the plugin's *display* name, spaces and
 *  all — hence the encode. */
const BASE = `http://localhost:1337/plugins/${encodeURIComponent("Decky Links")}/dist/sounds`;

const cache = new Map<SoundName, HTMLAudioElement>();

function element(name: SoundName): HTMLAudioElement {
  let audio = cache.get(name);
  if (!audio) {
    audio = new Audio(`${BASE}/${name}.flac`);
    audio.preload = "auto";
    cache.set(name, audio);
  }
  return audio;
}

/** Pull every sound into the decoder up front.
 *
 * This is what buys the 0.3ms: a cold element still has to fetch and decode,
 * and doing that at the moment a tag lands is how the feedback ends up late
 * again by a different route.
 */
export function preloadSounds(): void {
  for (const name of KNOWN) {
    try {
      element(name).load();
    } catch (e) {
      console.warn(`[ Decky Links ] Could not preload ${name}:`, e);
    }
  }
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
  try {
    const source = element(name as SoundName);
    const voice = new Audio(source.src);
    voice.volume = 1;
    void voice.play().catch((e) => {
      console.warn(`[ Decky Links ] Sound ${name} did not play:`, e?.name ?? e);
    });
  } catch (e) {
    console.warn(`[ Decky Links ] Sound ${name} failed:`, e);
  }
}
