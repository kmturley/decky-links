import { Scene } from "./presentation";
import { callable } from "@decky/api";

/** Themes, loaded at runtime from files rather than compiled in.
 *
 * A theme used to be a TypeScript module in this bundle, which meant writing
 * one required a checkout, a toolchain and a release. Since the entire point
 * of themes is that other people write them, a theme is now a folder:
 *
 *     themes/<id>/
 *         theme.html      manifest, styles and every screen
 *         sounds/*.flac   optional
 *
 * One file plus some audio. The HTML carries its own metadata in a JSON block
 * and each screen in a <template>, so there is no second file to keep in step
 * and no build between editing a theme and seeing it.
 *
 * Loaded from ~/Documents/decky-links/themes first and the plugin's own
 * assets/themes second, so a user can shadow a shipped theme by reusing its
 * id — the cheapest possible way to tweak one.
 */

export interface ThemeSummary {
  id: string;
  name: string;
  blurb: string;
  builtin: boolean;
}

/** Per-scene settings from the manifest. Everything is optional: a theme that
 *  says nothing about a scene still gets sensible behaviour. */
export interface SceneConfig {
  enterSound?: string;
  loopSound?: string;
  /** Hold off painting until the scene has lasted this long. For scenes that
   *  can pass in a blink — a tag reads in well under 200ms, and a screen that
   *  appears for two frames reads as a fault rather than a load. */
  minVisibleMs?: number;
  fadeMs?: number;
}

export interface Theme extends ThemeSummary {
  /** Scene name to markup, from the file's <template data-scene> blocks. */
  screens: Record<string, string>;
  /** Every <style> in the file, concatenated. */
  css: string;
  /** Replacements for the plugin's own event sounds. */
  sounds: Record<string, string>;
  scenes: Record<string, SceneConfig>;
}

const listThemes = callable<[], ThemeSummary[]>("list_themes");
const readTheme = callable<[theme_id: string], { html: string } | null>("read_theme");
const readThemeAsset =
  callable<[theme_id: string, name: string], string | null>("read_theme_asset");

let summaries: ThemeSummary[] = [];
const loaded = new Map<string, Theme | null>();

/** Parse a theme file into the pieces the layer renders.
 *
 * DOMParser rather than a regex, because the markup is a whole document and
 * hand-written by people who are not us. Note that neither <script> nor an
 * inline handler in a <template> can execute: templates are inert, and React
 * sets this markup through innerHTML, which never runs script. A theme is
 * markup, and stays markup.
 */
function parse(id: string, html: string, summary: ThemeSummary): Theme {
  const doc = new DOMParser().parseFromString(html, "text/html");

  let manifest: any = {};
  const block = doc.querySelector('script[type="application/json"]#decky-theme');
  if (block?.textContent) {
    try {
      manifest = JSON.parse(block.textContent);
    } catch (e) {
      console.warn(`[ Decky Links ] Theme ${id} has an unreadable manifest:`, e);
    }
  }

  const screens: Record<string, string> = {};
  doc.querySelectorAll("template[data-scene]").forEach((el) => {
    const scene = el.getAttribute("data-scene");
    if (scene) screens[scene] = (el as HTMLTemplateElement).innerHTML;
  });

  const css = [...doc.querySelectorAll("style")].map((s) => s.textContent ?? "").join("\n");

  return {
    ...summary,
    name: manifest.name || summary.name,
    blurb: manifest.blurb || summary.blurb,
    screens,
    css,
    sounds: manifest.sounds ?? {},
    scenes: manifest.scenes ?? {},
  };
}

/** Refresh the list of installed themes. */
export async function discoverThemes(): Promise<ThemeSummary[]> {
  try {
    summaries = (await listThemes()) ?? [];
  } catch (e) {
    console.warn("[ Decky Links ] Could not list themes:", e);
    summaries = [];
  }
  return summaries;
}

export function knownThemes(): ThemeSummary[] {
  return summaries;
}

/** Load a theme's markup, once.
 *
 * Cached including the failure: a theme that will not parse must not be
 * re-fetched on every scene change, and the layer treats "no theme" the same
 * way whichever direction it arrived from.
 */
export async function loadTheme(id: string): Promise<Theme | null> {
  if (loaded.has(id)) return loaded.get(id) ?? null;

  const summary = summaries.find((t) => t.id === id)
    ?? { id, name: id, blurb: "", builtin: false };
  let theme: Theme | null = null;
  try {
    const file = await readTheme(id);
    if (file?.html) theme = parse(id, file.html, summary);
  } catch (e) {
    console.warn(`[ Decky Links ] Could not load theme ${id}:`, e);
  }
  loaded.set(id, theme);
  return theme;
}

/** Forget everything, so an edited theme shows up without a plugin restart.
 *
 * The point of a file-based format is the edit-and-look loop; a cache with no
 * way to clear it would put a restart back in the middle of that.
 */
export function forgetThemes(): void {
  loaded.clear();
}

/** The markup for a scene, or the theme's standby screen.
 *
 * The fallback is *within* the theme rather than to another one: a theme that
 * does not draw an ambient screen should idle on its own standby screen, not
 * borrow a stranger's. Falling through to Steam would be worse still — a gap
 * in a takeover is the thing this feature exists to prevent.
 */
export function screenFor(theme: Theme | null, scene: Scene): string | null {
  if (!theme) return null;
  return theme.screens[scene] ?? theme.screens[Scene.READY] ?? null;
}

export function configFor(theme: Theme | null, scene: Scene): SceneConfig {
  return theme?.scenes?.[scene] ?? {};
}

/** Fill in the handful of values a screen may ask for.
 *
 * Substitution rather than templating on purpose: a theme is markup written by
 * hand, and two placeholders someone can remember beat a syntax they have to
 * learn. Values are escaped — a game title is not markup, and "Command &
 * Conquer" should not be able to close a tag.
 */
export function fillScreen(markup: string, values: Record<string, string>): string {
  return markup.replace(/\{(\w+)\}/g, (whole, key) => {
    const value = values[key];
    if (value === undefined) return whole;
    return value
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  });
}

const assets = new Map<string, string>();

/** A blob URL for one of a theme's sounds.
 *
 * Themes in Documents are not served over HTTP — only the plugin's own dist/
 * is — so the bytes come over the RPC channel and become a blob here. Cached
 * by theme and name: the seek loop is fetched once, not once per disk.
 */
export async function themeAssetUrl(themeId: string, file: string): Promise<string | null> {
  const key = `${themeId}/${file}`;
  const cached = assets.get(key);
  if (cached) return cached;

  try {
    const b64 = await readThemeAsset(themeId, file);
    if (!b64) return null;
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes]));
    assets.set(key, url);
    return url;
  } catch (e) {
    console.warn(`[ Decky Links ] Could not read ${key}:`, e);
    return null;
  }
}
