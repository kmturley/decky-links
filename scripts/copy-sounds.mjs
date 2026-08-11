/** Copy assets/sounds/*.flac into dist/sounds/.
 *
 * dist/ is the only directory Decky Loader serves over HTTP — verified on a
 * Deck: dist/index.js is 200, main.py is 403, assets/logo.png is 404. Sounds
 * now play in the frontend (see src/lib/sounds.ts), so they have to be
 * reachable by URL, which means they have to be in dist/.
 *
 * assets/ is still the source of truth and still vendored into py_modules/ by
 * build.sh; this is a copy for delivery, not a move.
 */
import { cp, mkdir, readdir } from "node:fs/promises";

await mkdir("dist/sounds", { recursive: true });
const names = (await readdir("assets/sounds")).filter((n) => n.endsWith(".flac"));
for (const name of names) {
  await cp(`assets/sounds/${name}`, `dist/sounds/${name}`);
}

// Theme assets go the same way and for the same reason: a theme's sounds are
// fetched by URL at runtime, so they have to be somewhere Decky will serve.
await cp("assets/themes", "dist/themes", { recursive: true });
const themes = await readdir("assets/themes");

console.log(`copied ${names.length} sounds and ${themes.length} theme(s) into dist/`);
