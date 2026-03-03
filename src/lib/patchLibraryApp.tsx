import { routerHook } from "@decky/api";
import GamePagePairer from "../GamePagePairer";

// simple route patch that wraps the entire tree and injects our pairer
// component at a fixed position.  this avoids any DOM hunting and ensures the
// button appears as soon as the game detail page renders.
export default function patchLibraryApp() {
  return routerHook.addPatch("/library/app/:appid", (route: any) => {
    try {
      // `route` is the RouteProps object; its `.children` property contains the
      // actual React element tree for the page.  we create a new object with the
      // same shape but with modified children.
      const tree = route?.children;
      if (!tree) {
        // nothing to patch yet - just return route unmodified.
        return route;
      }

      const patchedChildren = (
        <>
          {tree}
          <div style={{ position: "absolute", top: 40, right: 15, zIndex: 10000 }}>
            <GamePagePairer embedded />
          </div>
        </>
      );

      return { ...route, children: patchedChildren };
    } catch (e) {
      console.error("[Decky Links] patchLibraryApp error", e, route);
      return route;
    }
  });
}
