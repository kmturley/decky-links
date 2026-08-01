import { FC, useEffect } from "react";
import { useViewedApp } from "./hooks/useAppId";
import { setViewedApp } from "./shared";

/**
 * Renders nothing. Publishes the game detail page currently on screen into
 * `sharedState.viewedApp` so the Quick Access panel can offer to pair it.
 *
 * Route params are only readable from inside the route's own React tree, so
 * the panel cannot determine the viewed game by itself — this bridges the two.
 * Mounted by the `/library/app/:appid` patch, so it unmounts (and clears the
 * state) automatically when the user navigates away.
 *
 * Kept separate from GamePagePairer deliberately: that component has its own
 * visibility rules (fullscreen transitions, DOM anchor hunting) and reporting
 * the viewed app must not be coupled to whether its icon happens to render.
 */
const ViewedAppReporter: FC = () => {
  const viewedApp = useViewedApp();

  useEffect(() => {
    setViewedApp(viewedApp);
  }, [viewedApp?.appId, viewedApp?.launchTarget, viewedApp?.name]);

  useEffect(() => {
    return () => setViewedApp(null);
  }, []);

  return null;
};

export default ViewedAppReporter;
