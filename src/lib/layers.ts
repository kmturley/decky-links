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

/** The custom-visuals layer: home, loading, error screens. */
export const LAYER_VISUALS = 7000;

/** The pair icon on a game's page. Below the visuals layer, because while a
 *  theme is up the game page it belongs to is not on screen — an icon floating
 *  over a DOS prompt is an offer to pair with something the user cannot see. */
export const LAYER_PAIR_ICON = 6900;
