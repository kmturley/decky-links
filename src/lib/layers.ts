/** Stacking order for everything this plugin puts on screen.
 *
 * Collected in one place because z-index arguments are only ever settled by
 * looking at both numbers at once, and they were previously a thousand lines
 * apart in two files that never import each other.
 *
 * Above all of these, and not ours to set: Steam's Quick Access menu and its
 * system dialogs, which render above the visuals layer. That is what keeps a
 * full-screen takeover escapable, and it is verified on hardware rather than
 * assumed — see VisualsLayer.
 */

/** The theme layer: home, loading, error screens.
 *
 * 9000 rather than 7000, measured on a Deck. Steam keeps a full-width 42px
 * strip along the bottom of the Big Picture window at z-index 7000 — invisible
 * while it has nothing to say, and it tied with this layer, so DOM order
 * decided and Steam won. Nothing showed, but the bottom of a theme was still
 * Steam's to hit-test, and hit testing is the whole point now that the layer
 * blocks input: a takeover with a live strip along one edge is worse than one
 * that never claimed to block anything.
 *
 * The ceiling is 10000, which is where Steam's popup portals sit — context
 * menus and dropdowns among them. Staying below that is deliberate and is what
 * keeps a full-screen takeover escapable, so raise this no further.
 */
export const LAYER_VISUALS = 9000;

/** The pair icon on a game's page. Below the visuals layer, because while a
 *  theme is up the game page it belongs to is not on screen — an icon floating
 *  over a DOS prompt is an offer to pair with something the user cannot see. */
export const LAYER_PAIR_ICON = 6900;
