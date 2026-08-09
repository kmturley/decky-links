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

const from = "assets/sounds";
const to = "dist/sounds";

await mkdir(to, { recursive: true });
const names = (await readdir(from)).filter((n) => n.endsWith(".flac"));
for (const name of names) {
  await cp(`${from}/${name}`, `${to}/${name}`);
}
console.log(`copied ${names.length} sounds into ${to}`);
